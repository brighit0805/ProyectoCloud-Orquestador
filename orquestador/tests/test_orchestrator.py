import copy
import unittest

from slice_orchestrator import composicion
from slice_orchestrator.driver_linux import DriverLinux
from slice_orchestrator.models import Host, EstadoSlice, TipoTopologia
from slice_orchestrator.network_manager import VLANAllocator
from slice_orchestrator.orchestrator import (
    DespliegueFallidoError,
    EdicionEnVivoError,
    desplegar_slice,
    editar_slice_desplegado,
    eliminar_slice,
)
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

    def test_bus_usa_una_sola_vlan_con_driver_linux(self):
        """Bus (13-nov) con DriverLinux: a diferencia de lineal/anillo (una
        VLAN por enlace), las 3 VMs deben compartir UNA sola VLAN/bridge --
        validado también con el driver real contra el clúster de PUCP, no
        solo en simulación (ver estado-proyecto, §9sexies)."""
        hosts = inventario()
        driver = DriverLinux()
        vlans = VLANAllocator(3000, 3010)
        slc = desplegar_slice(
            "sb", "bus", "u", TipoTopologia.BUS, 3, hosts, "az-lab",
            driver=driver, vlan_allocator=vlans,
        )
        self.assertEqual(slc.estado, EstadoSlice.ACTIVO)
        self.assertEqual(len(slc.enlaces), 2)                      # n-1, igual forma que lineal
        self.assertEqual(len({e.vlan_id for e in slc.enlaces}), 1)  # pero UNA sola VLAN compartida
        self.assertEqual(len({e.nombre_red for e in slc.enlaces}), 1)
        eliminar_slice(slc, hosts, driver, vlans)
        self.assertEqual(slc.estado, EstadoSlice.ELIMINADO)
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


class PruebasEdicionEnVivo(unittest.TestCase):
    """editar_slice_desplegado (13-nov): reconciliación incremental sobre un
    slice ya ACTIVO -- solo se crea/conecta lo agregado y se borra/desconecta
    lo quitado, sin tumbar lo que no cambió."""

    def _slice_lineal(self, n=3):
        hosts = inventario()
        driver = DriverLinux()
        vlans = VLANAllocator(3000, 3010)
        slc = desplegar_slice(
            "ev", "ev", "u", TipoTopologia.LINEAL, n, hosts, "az-lab",
            driver=driver, vlan_allocator=vlans,
        )
        return slc, hosts, driver, vlans

    def test_agrega_nodo_y_enlace_sin_tocar_lo_existente(self):
        slc, hosts, driver, vlans = self._slice_lineal(3)
        vms_antes = set(driver.vms_simuladas)
        redes_antes = set(driver.redes_simuladas)
        nuevo = copy.deepcopy(slc)
        n3 = composicion.agregar_nodo(nuevo)
        composicion.agregar_enlace(nuevo, nuevo.nodos[-2].id, n3.id)   # cuelga del último viejo

        resultado = editar_slice_desplegado(slc, nuevo, hosts, driver, vlans)

        self.assertEqual(resultado.estado, EstadoSlice.ACTIVO)
        self.assertEqual(len(resultado.nodos), 4)
        self.assertEqual(len(resultado.enlaces), 3)
        # Las VMs/redes que ya existían siguen intactas (no se recrearon)
        self.assertTrue(vms_antes.issubset(driver.vms_simuladas))
        self.assertTrue(redes_antes.issubset(driver.redes_simuladas))
        # Y hay una VM nueva de más, y la red del enlace nuevo quedó creada
        # (en 1 o 2 hosts según en cuál(es) caiga cada extremo)
        self.assertEqual(len(driver.vms_simuladas), len(vms_antes) + 1)
        nombre_red_nuevo = resultado.enlaces[-1].nombre_red
        self.assertTrue(any(nombre == nombre_red_nuevo for _, nombre in driver.redes_simuladas))
        self.assertGreater(len(driver.redes_simuladas), len(redes_antes))
        nodo_nuevo = resultado.nodo(n3.id)
        self.assertIsNotNone(nodo_nuevo.host_id)
        eliminar_slice(resultado, hosts, driver, vlans)

    def test_quita_nodo_y_enlace_sin_tocar_lo_que_sobrevive(self):
        slc, hosts, driver, vlans = self._slice_lineal(3)
        vcpu_antes = sum(h.vcpu_usados for h in hosts)
        nuevo = copy.deepcopy(slc)
        extremo = nuevo.nodos[-1]                      # último nodo de la cadena lineal
        composicion.eliminar_nodo(nuevo, extremo.id)    # se lleva su único enlace

        resultado = editar_slice_desplegado(slc, nuevo, hosts, driver, vlans)

        self.assertEqual(resultado.estado, EstadoSlice.ACTIVO)
        self.assertEqual(len(resultado.nodos), 2)
        self.assertEqual(len(resultado.enlaces), 1)
        self.assertEqual(sum(h.vcpu_usados for h in hosts), vcpu_antes - extremo.vcpu)
        self.assertFalse(any(nombre == extremo.nombre for _, nombre in driver.vms_simuladas))
        eliminar_slice(resultado, hosts, driver, vlans)

    def test_fallo_al_agregar_revierte_solo_lo_agregado(self):
        slc, hosts, driver, vlans = self._slice_lineal(3)
        vms_antes = set(driver.vms_simuladas)
        redes_antes = set(driver.redes_simuladas)
        vcpu_antes = sum(h.vcpu_usados for h in hosts)
        nuevo = copy.deepcopy(slc)
        n3 = composicion.agregar_nodo(nuevo)
        composicion.agregar_enlace(nuevo, nuevo.nodos[-2].id, n3.id)

        driver.fallar_en_operacion = driver._numero_operacion + 1  # falla la próxima operación (crear_red)
        with self.assertRaises(EdicionEnVivoError):
            editar_slice_desplegado(slc, nuevo, hosts, driver, vlans)

        # Nada de lo que ya existía se tocó, y no quedó huérfano lo agregado
        self.assertEqual(driver.vms_simuladas, vms_antes)
        self.assertEqual(driver.redes_simuladas, redes_antes)
        self.assertEqual(sum(h.vcpu_usados for h in hosts), vcpu_antes)
        eliminar_slice(slc, hosts, driver, vlans)


if __name__ == "__main__":
    unittest.main()
