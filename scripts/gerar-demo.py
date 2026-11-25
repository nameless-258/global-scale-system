"""Gera a versão de demonstração da interface (pasta demo/) para o GitHub Pages.

A demonstração é a MESMA interface de ui/, mais o simulador (demo/simulador.js),
que responde aos pedidos no próprio browser em vez de um servidor. Corre-se
depois de alterar a interface:

    python scripts/gerar-demo.py

Publicação: o conteúdo de demo/ vai para o ramo gh-pages
    git subtree push --prefix demo origin gh-pages
"""
import re
import shutil
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
UI, DEMO = RAIZ / "ui", RAIZ / "demo"

shutil.copy(UI / "app.js", DEMO / "app.js")
shutil.copy(UI / "app.css", DEMO / "app.css")
shutil.copytree(UI / "fonts", DEMO / "fonts", dirs_exist_ok=True)

html = (UI / "index.html").read_text(encoding="utf-8")
FAIXA = """<div class="faixa-demo" role="note"><span><b>Demonstração.</b> Esta página simula o sistema no próprio browser:
não há servidores, bases de dados nem HDFS por trás, e os dados recomeçam quando recarrega a página.</span>
<a href="https://github.com/nameless-258/global-scale-system">O sistema real corre com Docker: ver o repositório</a></div>"""
CREDENCIAIS = """<div class="credenciais-demo">Utilizadores de demonstração:
<button type="button" data-u="ana" data-p="Ana@2026">ana</button> (cliente),
<button type="button" data-u="diego" data-p="Diego@2026">diego</button> (cliente),
<button type="button" data-u="admin" data-p="Admin@2026">admin</button> (administrador).
As palavras-passe estão no README: <code>Ana@2026</code>, <code>Diego@2026</code>, <code>Admin@2026</code>.</div>"""
PREENCHER = """<script>document.querySelectorAll(".credenciais-demo [data-u]").forEach((b) => b.addEventListener("click", () => {
  document.getElementById("login-user").value = b.dataset.u; document.getElementById("login-pass").value = b.dataset.p;
  document.getElementById("login-pass").focus(); }));</script>"""

def trocar(a, b):
    global html
    assert html.count(a) == 1, a
    html = html.replace(a, b)

trocar('<link rel="stylesheet" href="app.css">', '<link rel="stylesheet" href="app.css">\n<link rel="stylesheet" href="demo.css">')
trocar("<body>", "<body>\n" + FAIXA)
trocar('<p class="erro-texto" id="login-erro" role="alert"></p>', CREDENCIAIS + '\n    <p class="erro-texto" id="login-erro" role="alert"></p>')
trocar('<script src="app.js"></script>', '<script src="simulador.js"></script>\n<script src="app.js"></script>\n' + PREENCHER)
html = re.sub(r"<title>(.*?)</title>", r"<title>\1 (demonstração)</title>", html, count=1)
(DEMO / "index.html").write_text(html, encoding="utf-8")
(DEMO / ".nojekyll").write_text("")
print("demo/ gerada:", sorted(p.name for p in DEMO.iterdir()))
