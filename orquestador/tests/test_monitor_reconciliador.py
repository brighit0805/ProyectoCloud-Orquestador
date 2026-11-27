"""
test_monitor_reconciliador.py
-------------------------------
HLD dominio D (Monitoreo) -- reconciliación de solo lectura: compara lo que
el orquestador tiene registrado como ACTIVO contra lo que el driver confirma
como real (``verificar_vm``), sin corregir nada por su cuenta.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slice_orchestrator import composicion
from slice_orchestrator.models import EstadoSlice, Host, TipoTopologia
from slice_orchestrator.monitor_reconciliador import (
    DiscrepanciaNodo, ReporteReconciliacion, reconciliar,
)
from slice_orchestrator.network_manager import VLANAllocator, asignar_redes
from slice_orchestrator.topology_manager import construir_topologia

HOSTS = [
    Host(id="server1", nombre="server1", uri_libvirt="qemu+ssh://u@s1/system",
         capacidad_vcpu=16, capacidad_ram_mb=16384),
    Host(id="server2", nombre="server2", uri_libvirt="qemu+ssh://u@s2/system",
         capacidad_vcpu=16, capacidad_ram_mb=16384),
]


def slice_activo(slice_id: str, n: int, hosts: list[str]) -> "Slice":
    slc = construir_topologia(slice_id, slice_id, "ana", TipoTopologia.LINEAL, n)
    asignar_redes(slc, VLANAllocator())
    for nodo, host in zip(slc.nodos, hosts):
        nodo.host_id = host
    slc.estado = EstadoSlice.ACTIVO
    return slc


class DriverFalso:
    """Driver mínimo con ``verificar_vm`` controlable por test, sin tocar
    libvirt/SSH real -- mismo espíritu que los drivers simulados de otros
    módulos (ver DriverLinux en modo SIMULADO)."""

    def __init__(self, estados: dict[str, str] | None = None):
        self.estados = estados or {}  # nombre_vm -> "ACTIVE"/"SHUTOFF"/"ERROR"
        self.consultas: list[tuple[str, str]] = []

    def verificar_vm(self, host_uri: str, nombre_vm: str) -> str:
        self.consultas.append((host_uri, nombre_vm))
        return self.estados.get(nombre_vm, "ACTIVE")


class DriverSinSoporte:
    """Un driver (incompleto o antiguo) que no implementa verificar_vm."""


class TestReconciliar(unittest.TestCase):
    def test_sin_slices_activos_es_ok(self):
        self.assertEqual(reconciliar([], HOSTS, DriverFalso(), composicion.clave_de), [])

    def test_slice_desplegado_coincide_con_lo_esperado(self):
        slc = slice_activo("s1", 2, ["server1", "server2"])
        reportes = reconciliar([slc], HOSTS, DriverFalso(), composicion.clave_de)
        self.assertEqual(len(reportes), 1)
        r = reportes[0]
        self.assertTrue(r.coincide)
        self.assertEqual(r.discrepancias, [])
        self.assertIsNone(r.error)

    def test_detecta_vm_desaparecida_por_fuera_del_orquestador(self):
        slc = slice_activo("s1", 2, ["server1", "server2"])
        vm_caida = slc.nodos[1].nombre
        driver = DriverFalso(estados={vm_caida: "ERROR"})
        r = reconciliar([slc], HOSTS, driver, composicion.clave_de)[0]
        self.assertFalse(r.coincide)
        self.assertEqual(len(r.discrepancias), 1)
        d = r.discrepancias[0]
        self.assertEqual((d.nombre_vm, d.real, d.esperado), (vm_caida, "ERROR", "ACTIVE"))

    def test_no_reconcilia_slices_en_borrador_ni_ya_eliminados(self):
        activo = slice_activo("s1", 2, ["server1", "server2"])
        borrador = construir_topologia("s2", "s2", "ana", TipoTopologia.LINEAL, 2)
        driver = DriverFalso()
        reportes = reconciliar([activo, borrador], HOSTS, driver, composicion.clave_de)
        self.assertEqual([r.slice_id for r in reportes], ["s1"])
        # el driver nunca se consulta para el slice en BORRADOR
        self.assertTrue(all(vm != borrador.nodos[0].nombre for _, vm in driver.consultas))

    def test_no_redespliega_nada_por_si_solo(self):
        """Reconciliar es de SOLO LECTURA: ni siquiera con discrepancias
        reales llama a nada que cree, borre o modifique una VM -- el driver
        falso aquí no tiene ningún otro método que crear_vm()/eliminar_vm(),
        y si reconciliar() intentara llamarlos, la prueba fallaría con
        AttributeError en vez de pasar."""
        slc = slice_activo("s1", 2, ["server1", "server2"])
        driver = DriverFalso(estados={slc.nodos[1].nombre: "ERROR"})
        r = reconciliar([slc], HOSTS, driver, composicion.clave_de)[0]
        self.assertFalse(r.coincide)   # detectó la discrepancia...
        # ...pero el slice/nodo en memoria no se tocó para nada
        self.assertEqual(slc.estado, EstadoSlice.ACTIVO)

    def test_driver_sin_soporte_se_reporta_explicitamente(self):
        slc = slice_activo("s1", 2, ["server1", "server2"])
        reportes = reconciliar([slc], HOSTS, DriverSinSoporte(), composicion.clave_de)
        self.assertEqual(len(reportes), 1)
        r = reportes[0]
        self.assertFalse(r.coincide)
        self.assertIsNotNone(r.error)
        self.assertIn("verificar_vm", r.error)

    def test_host_desconocido_se_reporta_sin_reventar(self):
        slc = slice_activo("s1", 2, ["server1", "server2"])
        slc.nodos[0].host_id = "server-fantasma"
        r = reconciliar([slc], HOSTS, DriverFalso(), composicion.clave_de)[0]
        self.assertFalse(r.coincide)
        self.assertEqual(r.discrepancias[0].real, "HOST_DESCONOCIDO")


if __name__ == "__main__":
    unittest.main()
