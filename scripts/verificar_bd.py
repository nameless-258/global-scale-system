import httpx
c = httpx.Client(base_url="https://gateway", verify="/certs/publico/ca.crt", timeout=30)
t = c.post("/auth/login", json={"username": "admin", "password": "Admin@2026"}).json()["access_token"]
h = {"Authorization": f"Bearer {t}"}
r = c.get("/admin/bd", headers=h); print("GET /admin/bd", r.status_code)
for n in r.json()["nos"]:
    print(f'{n["no"]:13} {n["estado"]:5} {n.get("papel","-"):9} contas={n.get("n_contas")} mov={n.get("n_movimentos")} lsn={n.get("lsn")} {n.get("erro","")}')
d = c.get("/admin/bd/db-africa", headers=h).json()
print("db-africa:", [(k["id"], k["titular_em_claro"], k["titular_cifrado"][:16], k["titular_decifrado"]) for k in d["contas"]], "taxas", len(d["taxas"]), "repl", d["replicacao"])
print("inexistente:", c.get("/admin/bd/xpto", headers=h).status_code)
ta = c.post("/auth/login", json={"username": "ana", "password": "Ana@2026"}).json()["access_token"]
print("cliente:", c.get("/admin/bd", headers={"Authorization": f"Bearer {ta}"}).status_code)
