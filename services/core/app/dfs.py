"""Acesso ao Sistema de Ficheiros Distribuído (HDFS via WebHDFS).

O HDFS dá TRANSPARÊNCIA DE LOCALIZAÇÃO: a aplicação pede "/auditoria/x.log" ao
NameNode (que guarda só os metadados) e é redirecionada (HTTP 307) para o
DataNode que tem o bloco. Os blocos são replicados (dfs.replication=2), pelo
que a perda de um DataNode não torna os ficheiros indisponíveis.

Concorrência: o HDFS usa o modelo "single-writer / multiple-readers" com
LEASES (arrendamentos) concedidos pelo NameNode. Só um cliente pode escrever
num ficheiro de cada vez; leitores concorrentes são sempre permitidos. Por isso
cada nó da API escreve no SEU ficheiro de auditoria e, dentro do processo, as
escritas são serializadas com um lock.

Existe também um backend "local" (pasta montada) para testes sem Hadoop.
"""
import fcntl
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import config
from .security import forma_canonica, sha256_hex


class DFSErro(Exception):
    pass


class WebHDFS:
    def __init__(self, base_url: str, user: str):
        self.base = base_url.rstrip("/")
        self.user = user
        self.http = httpx.Client(timeout=10)

    def _url(self, path: str, op: str, **params) -> str:
        q = "&".join([f"op={op}", f"user.name={self.user}"] +
                     [f"{k}={v}" for k, v in params.items()])
        return f"{self.base}/webhdfs/v1{path}?{q}"

    def _check(self, r: httpx.Response, *ok):
        if r.status_code not in ok:
            raise DFSErro(f"WebHDFS {r.request.method} {r.request.url.path} -> "
                          f"{r.status_code}: {r.text[:300]}")
        return r

    def mkdirs(self, path: str):
        self._check(self.http.put(self._url(path, "MKDIRS")), 200)

    def _no_datanode(self, metodo: str, location: str, sucesso: tuple, **kw) -> httpx.Response:
        """Envia o pedido ao DataNode indicado pelo NameNode. Se esse DataNode estiver
        em baixo (o NameNode só o declara morto ao fim de ~60 s), tenta os outros
        DataNodes: qualquer DataNode serve o pedido, porque o seu cliente HDFS interno
        lê/escreve nas réplicas vivas. Tolerância a falhas do lado do cliente."""
        url = httpx.URL(location)
        alvos = [url] + [url.copy_with(host=h.split(":")[0], port=int(h.split(":")[1]))
                         for h in config.DFS_DATANODES if h != f"{url.host}:{url.port}"]
        ultimo_erro = None
        for alvo in alvos:
            try:
                r = self.http.request(metodo, alvo, **kw)
                if r.status_code in sucesso:
                    return r
                ultimo_erro = DFSErro(f"{alvo.host} -> {r.status_code}: {r.text[:200]}")
            except httpx.TransportError as e:            # DataNode inacessível
                ultimo_erro = e
        raise DFSErro(f"Nenhum DataNode respondeu: {ultimo_erro}")

    def escrever(self, path: str, dados: bytes, overwrite: bool = True):
        # Passo 1: o NameNode decide em que DataNode escrever (307 Redirect)
        r = self._check(self.http.put(self._url(path, "CREATE",
                                                overwrite=str(overwrite).lower())), 307)
        # Passo 2: os dados são enviados diretamente para o DataNode
        self._no_datanode("PUT", r.headers["location"], (201,), content=dados,
                          headers={"Content-Type": "application/octet-stream"})

    def anexar(self, path: str, dados: bytes):
        r = self.http.post(self._url(path, "APPEND"))
        if r.status_code == 404:                       # ficheiro ainda não existe
            return self.escrever(path, dados, overwrite=False)
        self._check(r, 307)
        self._no_datanode("POST", r.headers["location"], (200,), content=dados,
                          headers={"Content-Type": "application/octet-stream"})

    def ler(self, path: str) -> bytes:
        r = self.http.get(self._url(path, "OPEN"))
        if r.status_code == 404:
            raise FileNotFoundError(path)
        if r.status_code == 200:
            return r.content
        self._check(r, 307)
        return self._no_datanode("GET", r.headers["location"], (200,)).content

    def listar(self, path: str) -> list[dict]:
        r = self.http.get(self._url(path, "LISTSTATUS"))
        if r.status_code == 404:
            return []
        estados = self._check(r, 200).json()["FileStatuses"]["FileStatus"]
        return [{"nome": s["pathSuffix"], "tipo": s["type"], "tamanho": s["length"],
                 "replicacao": s.get("replication"),
                 "modificado": datetime.fromtimestamp(s["modificationTime"] / 1000,
                                                      timezone.utc).isoformat()}
                for s in estados]

    def saude(self) -> dict:
        r = self.http.get(f"{self.base}/jmx?qry=Hadoop:service=NameNode,name=FSNamesystemState")
        b = self._check(r, 200).json()["beans"][0]
        return {"backend": "HDFS", "namenode": self.base,
                "datanodes_vivos": b.get("NumLiveDataNodes"),
                "datanodes_mortos": b.get("NumDeadDataNodes"),
                "estado": b.get("FSState")}


class DFSLocal:
    """Backend de recurso: pasta partilhada (ex: montagem NFS)."""

    def __init__(self, raiz: str):
        self.raiz = Path(raiz)
        self.raiz.mkdir(parents=True, exist_ok=True)

    def _p(self, path: str) -> Path:
        return self.raiz / path.lstrip("/")

    def mkdirs(self, path):
        self._p(path).mkdir(parents=True, exist_ok=True)

    def escrever(self, path, dados, overwrite=True):
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and not overwrite:
            raise DFSErro("ficheiro já existe")
        p.write_bytes(dados)

    def anexar(self, path, dados):
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "ab") as f:
            fcntl.lockf(f, fcntl.LOCK_EX)        # lock POSIX (funciona também sobre NFSv4)
            f.write(dados)
            f.flush()
            os.fsync(f.fileno())
            fcntl.lockf(f, fcntl.LOCK_UN)

    def ler(self, path):
        p = self._p(path)
        if not p.exists():
            raise FileNotFoundError(path)
        return p.read_bytes()

    def listar(self, path):
        p = self._p(path)
        if not p.exists():
            return []
        return [{"nome": c.name, "tipo": "DIRECTORY" if c.is_dir() else "FILE",
                 "tamanho": c.stat().st_size, "replicacao": 1,
                 "modificado": datetime.fromtimestamp(c.stat().st_mtime, timezone.utc).isoformat()}
                for c in sorted(p.iterdir())]

    def saude(self):
        return {"backend": "local", "raiz": str(self.raiz), "estado": "Operational"}


def criar_dfs():
    if config.DFS_BACKEND == "local":
        return DFSLocal(config.DFS_LOCAL_PATH)
    return WebHDFS(config.DFS_URL, config.DFS_USER)


class AuditoriaEncadeada:
    """Registo de auditoria à prova de adulteração (hash chain, como num blockchain).

    Cada entrada contém o hash da entrada anterior; alterar qualquer linha
    invalida todas as seguintes. Cada processo escreve num ficheiro novo
    (/auditoria/<no>/<arranque>.log) — evita conflitos de lease do HDFS após um
    crash e facilita a verificação.
    """

    def __init__(self, dfs, no: str):
        self.dfs = dfs
        self.no = no
        self.lock = threading.Lock()
        self.seq = 0
        self.hash_anterior = "0" * 64           # bloco génese
        arranque = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.caminho = f"/auditoria/{no}/{arranque}.log"
        self.pendentes: list[bytes] = []        # buffer se o DFS estiver em baixo
        self.ultimo_erro: str | None = None

    def registar(self, evento: str, **dados):
        with self.lock:
            self.seq += 1
            entrada = {"seq": self.seq, "ts": datetime.now(timezone.utc).isoformat(),
                       "no": self.no, "evento": evento, "dados": dados,
                       "hash_anterior": self.hash_anterior}
            entrada["hash"] = sha256_hex(forma_canonica(entrada))
            self.hash_anterior = entrada["hash"]
            linha = (json.dumps(entrada, ensure_ascii=False) + "\n").encode()
            self.pendentes.append(linha)
            self._flush()

    def _flush(self):
        if not self.pendentes:
            return
        try:
            self.dfs.anexar(self.caminho, b"".join(self.pendentes))
            self.pendentes.clear()
            self.ultimo_erro = None
        except Exception as e:  # DFS indisponível: mantém em memória e tenta depois
            self.ultimo_erro = str(e)[:200]

    def tentar_flush(self):
        with self.lock:
            self._flush()


def verificar_cadeia(conteudo: bytes) -> dict:
    anterior = "0" * 64
    linhas = [l for l in conteudo.decode().splitlines() if l.strip()]
    for i, l in enumerate(linhas, 1):
        e = json.loads(l)
        h = e.pop("hash")
        if e["hash_anterior"] != anterior:
            return {"integro": False, "linha": i, "motivo": "encadeamento quebrado"}
        if sha256_hex(forma_canonica(e)) != h:
            return {"integro": False, "linha": i, "motivo": "conteúdo da entrada foi alterado"}
        anterior = h
    return {"integro": True, "entradas": len(linhas), "ultimo_hash": anterior}
