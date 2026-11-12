import unittest

from slice_orchestrator.driver_linux import DriverLinux
from slice_orchestrator.models import Host, EstadoSlice, TipoTopologia
from slice_orchestrator.network_manager import VLANAllocator
from slice_orchestrator.orchestrator import DespliegueFallidoError, desplegar_slice, eliminar_slice
from slice_orchestrator.topology_manager import construir_topologia
from slice_orchestrator.vm_placement import EstrategiaPlacement


class DriverLinuxConMonitoreo(DriverLinux):
    """DriverLinux de prueba que ademas sabe 'descubrir_hosts' (como
    DriverOpenStack), para comprobar que el orquestador SI propaga el
    driver hacia el Scheduler cuando la estrategia es OBJETIVO (Lab 7)."""

    def __init__(self, hosts_a_reportar, **kwargs):
        super().__init__(**kwargs)
        self._hosts_a_reportar = hosts_a_reportar
        self.llamadas_descubrir_hosts = 0

    def descubrir_hosts(self):
        self.llamadas_descubrir_hosts += 1
        return self._hosts_a_reportar


def inventario():
    return [
        Host("h1", "host-1", "sim://h1", 4, 4096, "az-lab"),
        Host("h2", "host-2", "sim://h2", 4, 4096, "az-lab"),
    ]


class PruebasOrquestador(unittest.TestCase):
    def test_topologias(self):
        lineal = construir_topologia("s1", "lineal", "u", TipoTopologia.LINEAL, 4)
        anillo = construir_topologia("s2", "anillo", "u", TipoTopologia.ANILLO, 4)
        self.assertEqual(len(lineal.enlaces), 3)
        self.assertEqual(len(anillo.enlaces), 4)
        self.assertEqual(anillo.enlaces[-1].nodo_destino, anillo.nodos[0].id)

    def test_ciclo_de_vida_libera_recursos(self):
        hosts = inventario()
        driver = DriverLinux()
        vlans = VLANAllocator(3000, 3010)
        slc = desplegar_slice(
            "s1", "lineal", "u", TipoTopologia.LINEAL, 4, hosts, "az-lab",
            driver=driver, vlan_allocator=vlans,
        )
        self.assertEqual(slc.estado, EstadoSlice.ACTIVO)
        self.assertEqual(len({n.host_id for n in slc.nodos}), 2)
        self.assertEqual(sum(h.vcpu_usados for h in hosts), 4)
        self.assertEqual(len({e.vlan_id for e in slc.enlaces}), 3)
        eliminar_slice(slc, hosts, driver, vlans)
        self.assertEqual(slc.estado, EstadoSlice.ELIMINADO)
        self.assertEqual(sum(h.vcpu_usados for h in hosts), 0)
        self.assertFalse(driver.vms_simuladas)
        self.assertFalse(driver.redes_simuladas)

    def test_vlans_no_se_repiten_entre_slices(self):
        hosts = inventario()
        driver = DriverLinux()
        vlans = VLANAllocator(3000, 3020)
        a = desplegar_slice("a", "a", "u", TipoTopologia.LINEAL, 3, hosts,
                            driver=driver, vlan_allocator=vlans)
        b = desplegar_slice("b", "b", "u", TipoTopologia.LINEAL, 3, hosts,
                            driver=driver, vlan_allocator=vlans)
        self.assertTrue({e.vlan_id for e in a.enlaces}.isdisjoint({e.vlan_id for e in b.enlaces}))

    def test_rollback_de_fallo_parcial(self):
        hosts = inventario()
        driver = DriverLinux(fallar_en_operacion=3)
        vlans = VLANAllocator(3000, 3010)
        with self.assertRaises(DespliegueFallidoError):
            desplegar_slice("fallo", "fallo", "u", TipoTopologia.LINEAL, 3, hosts,
                            driver=driver, vlan_allocator=vlans)
        self.assertEqual(sum(h.vcpu_usados for h in hosts), 0)
        self.assertFalse(driver.vms_simuladas)
        self.assertFalse(driver.redes_simuladas)
        self.assertNotIn("fallo", vlans._reservas)

    def test_estrategia_objetivo_propaga_driver_para_monitoreo_en_vivo(self):
        """Lab 7: al desplegar con estrategia OBJETIVO, el orquestador debe
        pasarle el driver a vm_placement.asignar_hosts para que el
        ResourceMonitor del Scheduler pueda refrescar el estado REAL de los
        hosts justo antes de decidir (y no solo usar los datos ya cacheados
        en la lista de Host que recibe). Antes de la correccion, el driver se
        descartaba y 'descubrir_hosts' nunca se llamaba."""
        hosts = inventario()
        driver = DriverLinuxConMonitoreo(hosts_a_reportar=hosts)
        vlans = VLANAllocator(3000, 3010)
        slc = desplegar_slice(
            "obj1", "obj1", "u", TipoTopologia.LINEAL, 2, hosts, "az-lab",
            driver=driver, vlan_allocator=vlans,
            estrategia=EstrategiaPlacement.OBJETIVO,
        )
        self.assertEqual(slc.estado, EstadoSlice.ACTIVO)
        self.assertGreaterEqual(driver.llamadas_descubrir_hosts, 1)


if __name__ == "__main__":
    unittest.main()
