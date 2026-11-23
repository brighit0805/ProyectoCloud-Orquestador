"""Demuestra el paralelismo de R3 CONTRA EL CLÚSTER REAL de OpenStack: instrumenta
crear_vm() para imprimir cuándo empieza y termina cada VM (con el nombre del hilo
que la maneja). Si los intervalos de las VMs se solapan en el tiempo, es prueba
directa de que corrieron en paralelo, no una tras otra.

No modifica ningún archivo del proyecto -- el "monkeypatch" solo vive mientras
corre este script. Debe ejecutarse en el headnode, dentro de orquestador/, con
las credenciales reales ya cargadas en la misma sesión de shell:

    . .env.real
    python3 demo_paralelismo_real.py
"""
import sys
import threading
import time

from slice_orchestrator.driver_openstack import ConfigOpenStack, DriverOpenStack, ModoOpenStack
from slice_orchestrator.servicio_slices import ServicioSlices
from slice_orchestrator.cli import ESTADO_POR_DEFECTO

# ---- 1) Instrumentar crear_vm() para ver inicio/fin de cada llamada -------
_original_crear_vm = DriverOpenStack.crear_vm


def _crear_vm_instrumentado(self, *args, **kwargs):
    nombre_vm = kwargs.get("nombre_vm") or (args[1] if len(args) > 1 else "?")
    hilo = threading.current_thread().name
    t0 = time.time()
    print(f"[t={t0:.3f}] INICIO crear_vm({nombre_vm})  hilo={hilo}", flush=True)
    try:
        return _original_crear_vm(self, *args, **kwargs)
    finally:
        t1 = time.time()
        print(f"[t={t1:.3f}] FIN    crear_vm({nombre_vm})  hilo={hilo}  duró {t1 - t0:.2f}s", flush=True)


DriverOpenStack.crear_vm = _crear_vm_instrumentado

# ---- 2) Armar el driver real (igual que hace cli.py) ----------------------
cfg = ConfigOpenStack.desde_entorno()
faltan = cfg.faltantes()
if faltan:
    sys.exit(f"Faltan variables de entorno: {faltan}. Corra primero '. .env.real'.")

driver = DriverOpenStack(modo=ModoOpenStack.REAL, config=cfg)
print("Descubriendo hosts reales...", flush=True)
hosts_reales = driver.descubrir_hosts()
svc = ServicioSlices(ruta_estado=ESTADO_POR_DEFECTO, hosts=hosts_reales, driver=driver)

# ---- 3) Desplegar un slice de 2 VMs contra el clúster real -----------------
spec = {
    "nombre": "demo-paralelo-real",
    "topologia": {"base": {"tipo": "lineal", "nodos": 2, "ramificacion": 2},
                  "injertos": [], "enlaces_extra": [], "enlaces_quitados": []},
    "flavores": {"vm1": "cirros", "vm2": "cirros"},
    "capacidad": {},
    "acceso_internet": [],
    "zona_disponibilidad": "nova",
}

print("\nDesplegando slice de 2 VMs contra el clúster REAL...\n", flush=True)
t_total0 = time.time()
d = svc.crear_slice("g2@pucp.edu.pe", spec, desplegar=True)
t_total1 = time.time()
print(f"\nSlice {d['id']} desplegado en {t_total1 - t_total0:.2f}s totales.")
print("\nSi ves que los INICIO/FIN de vm1 y vm2 se SOLAPAN en el tiempo "
      "(el INICIO de la segunda llega antes del FIN de la primera), es la "
      "prueba de que se crearon en paralelo contra el clúster real.")
print(f"\nPara limpiar: python3 -m slice_orchestrator.cli --driver openstack "
      f"--usuario g2@pucp.edu.pe slice eliminar {d['id']}")
