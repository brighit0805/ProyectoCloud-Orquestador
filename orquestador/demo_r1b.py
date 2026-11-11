"""
demo_r1b.py
-----------
Recorre, en orden, los 8 criterios de R1B (6 pts) y muestra la evidencia de cada
uno. No necesita clúster ni credenciales: usa el driver Linux en modo SIMULADO.

    python demo_r1b.py

La salida está pensada para capturarla como evidencia: cada bloque lleva el
criterio de la rúbrica que cubre.
"""

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

from slice_orchestrator.servicio_slices import ErrorServicio, ServicioSlices


def titulo(texto):
    print("\n" + "=" * 78 + f"\n{texto}\n" + "=" * 78)


def resumen(d):
    print(f"  {d['id']} «{d['nombre']}»  estado={d['estado']}  tipo={d['tipo']}  "
          f"nodos={len(d['nodos'])}  enlaces={len(d['enlaces'])}")


def main():
    svc = ServicioSlices()      # en memoria; pase una ruta para persistir en JSON

    titulo("[1 pt] Soportar concurrencia de usuarios — 8 usuarios crean y despliegan a la vez")

    def sesion(i):
        u = f"usuario{i}"
        return u, svc.crear_slice(u, {"nombre": "lab", "topologia": {"base": "anillo", "nodos": 3}},
                                  desplegar=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        resultados = list(pool.map(sesion, range(1, 9)))
    vlans = [e["vlan"] for _, d in resultados for e in d["enlaces"]]
    print(f"  slices activos: {len(resultados)} | VLAN asignadas: {len(vlans)} | repetidas: {len(vlans) - len(set(vlans))}")
    print("  usuario3 ve:", [s["id"] for s in svc.listar_slices("usuario3")], "(solo el suyo)")
    ajeno = resultados[0][1]["id"]
    try:
        svc.ver_slice("usuario3", ajeno)
    except ErrorServicio as exc:
        print(f"  usuario3 intenta ver {ajeno} de usuario1 → {exc}")
    for u, d in resultados:
        svc.eliminar_slice(u, d["id"])

    titulo("[0,5 pt] Configurar slices con topologías predefinidas (nodos, cores, RAM, disco)")
    for t in svc.listar_topologias():
        print(f"  {t['tipo']:7s} enlaces={t['enlaces']:9s} disponible={'sí' if t['disponible'] else 'no (diseñada)'}")
    print("  Flavors:", [(f['nombre'], f['vcpu'], f['ram_mb'], f['disco_gb']) for f in svc.listar_flavors()])
    d = svc.crear_slice("ana", {
        "nombre": "malla-4", "topologia": {"base": "malla", "nodos": 4},
        "capacidad": {"vm1": {"vcpu": 2, "ram_mb": 1024, "disco_gb": 8}}})
    resumen(d)
    vm1 = d["nodos"][0]
    print(f"  vm1 con capacidad propia: {vm1['vcpu']} vCPU, {vm1['ram_mb']} MB, {vm1['disco_gb']} GB ({vm1['flavor']})")
    svc.eliminar_slice("ana", d["id"])

    titulo("[2 pts] Componer y editar slices arbitrarias con apoyo de topologías predefinidas")
    d = svc.crear_slice("ana", {"nombre": "ex1", "topologia": {"base": "anillo", "nodos": 4},
                                "flavores": {"vm2": "cirros"},
                                "acceso_internet": ["vm1", "vm3"], "zona_disponibilidad": "az-lab"})
    print("  1) generar desde el catálogo: anillo de 4"); resumen(d)
    d = svc.editar_slice("ana", d["id"], [{"op": "injertar", "sobre": "vm4", "tipo": "lineal", "nodos": 2},
                                          {"op": "capacidad", "nodo": "vm6", "flavor": "cirros"}])
    print("  2) injertar una rama lineal de 2 sobre vm4 (= EX1-Topology) y poner vm6 en cirros"); resumen(d)
    d = svc.editar_slice("ana", d["id"], [{"op": "agregar_enlace", "a": "vm2", "b": "vm5"},
                                          {"op": "agregar_nodo", "capacidad": {"vcpu": 2}},
                                          {"op": "agregar_enlace", "a": "vm7", "b": "vm1"}])
    print("  3) enlazar vm2–vm5, agregar vm7 (2 vCPU) y enlazarlo a vm1"); resumen(d)
    d = svc.editar_slice("ana", d["id"], [{"op": "quitar_enlace", "a": "vm2", "b": "vm5"},
                                          {"op": "eliminar_nodo", "nodo": "vm6"}])
    print("  4) quitar el enlace vm2–vm5 y eliminar vm6"); resumen(d)
    print("  5) una edición inválida no deja nada a medias:")
    antes = svc.ver_slice("ana", d["id"])
    try:
        svc.editar_slice("ana", d["id"], [{"op": "agregar_nodo"}, {"op": "agregar_enlace", "a": "vm1", "b": "vm99"}])
    except ErrorServicio as exc:
        print(f"     rechazada: {exc}")
    print(f"     el slice sigue igual: {svc.ver_slice('ana', d['id']) == antes}")
    print("  6) un diseño desconectado no se despliega:")
    d = svc.editar_slice("ana", d["id"], [{"op": "agregar_nodo"}])
    try:
        svc.desplegar_slice("ana", d["id"])
    except ErrorServicio as exc:
        print(f"     rechazado: {exc}")
    d = svc.editar_slice("ana", d["id"], [{"op": "eliminar_nodo", "nodo": "vm8"}])
    plantilla_origen = d["id"]

    titulo("[0,5 pt] Editar, listar y eliminar slices")
    d = svc.desplegar_slice("ana", d["id"], estrategia="round_robin")
    print("  desplegado:"); resumen(d)
    print("  hosts:", {n["clave"]: n["host"] for n in d["nodos"]})
    for s in svc.listar_slices("ana"):
        print("  listar →", s)
    try:
        svc.editar_slice("ana", d["id"], [{"op": "agregar_nodo"}])
    except ErrorServicio as exc:
        print(f"  editar un slice activo → {exc}")

    titulo("[0,5 pt] Visualizar el consumo de recursos")
    r = svc.consumo_recursos("ana")
    for h in r["hosts"]:
        print(f"  {h['host']}: vCPU {h['vcpu_usados']}/{h['vcpu_total']}  RAM {h['ram_usada_mb']}/{h['ram_total_mb']} MB  "
              f"disco {h['disco_usado_gb']} GB")
    print("  total de ana:", r["total_mio"])

    titulo("[0,5 pt] Exportar plantilla e importarla para crear un slice nuevo")
    plantilla = svc.exportar_plantilla("ana", plantilla_origen)
    print(f"  plantilla: versión {plantilla['version']}, {len(plantilla['nodos'])} nodos, "
          f"{len(plantilla['enlaces'])} enlaces (sin VLAN, host, IP ni propietario)")
    copia = svc.crear_desde_plantilla("bob", json.loads(json.dumps(plantilla)), "copia-de-ana")
    resumen(copia)
    print("  bob la despliega sin tocar el de ana:", svc.desplegar_slice("bob", copia["id"])["estado"],
          "| el de ana:", svc.ver_slice("ana", plantilla_origen)["estado"])

    titulo("[0,5 pt] Importar imágenes")
    with tempfile.TemporaryDirectory() as tmp:
        ruta = os.path.join(tmp, "debian.qcow2")
        with open(ruta, "wb") as f:
            f.write(b"QFI\xfb" + b"\0" * 4096)
        i = svc.importar_imagen("ana", "debian-12.qcow2", ruta)
        print(f"  importada {i['nombre']}: {i['tamano_bytes']} bytes, sha256 {i['sha256'][:16]}…")
        with open(ruta, "wb") as f:
            f.write(b"no es una imagen")
        try:
            svc.importar_imagen("ana", "falsa.qcow2", ruta)
        except ErrorServicio as exc:
            print(f"  archivo que no es qcow2 → {exc}")
    print("  catálogo:", [x["nombre"] for x in svc.listar_imagenes()])
    d2 = svc.crear_slice("ana", {"nombre": "con-debian", "topologia": {"base": "lineal", "nodos": 2},
                                 "capacidad": {"vm1": {"imagen": "debian-12.qcow2"}}})
    print("  slice que usa la imagen importada: vm1 →", d2["nodos"][0]["imagen"])

    titulo("[0,5 pt] Tokens de acceso a la consola virtual de las VMs")
    t = svc.emitir_token_consola("ana", plantilla_origen, "vm3")
    print(f"  token emitido para vm3: host={t['host']} puerto={t['puerto']} caduca en {t['expira_en']} s "
          f"(simulado: {t['simulado']})")
    print("  canje 1:", svc.canjear_token_consola(t["token"]))
    try:
        svc.canjear_token_consola(t["token"])
    except ErrorServicio as exc:
        print(f"  canje 2 del mismo token → {exc}")
    try:
        svc.emitir_token_consola("bob", plantilla_origen, "vm3")
    except ErrorServicio as exc:
        print(f"  bob pide consola del slice de ana → {exc}")

    print("\nFin. Todo en modo SIMULADO: no se tocó ningún clúster.")


if __name__ == "__main__":
    import sys
    for _flujo in (sys.stdout, sys.stderr):      # consola de Windows (cp1252): evitar UnicodeEncodeError
        if hasattr(_flujo, "reconfigure"):
            _flujo.reconfigure(encoding="utf-8", errors="replace")
    main()
