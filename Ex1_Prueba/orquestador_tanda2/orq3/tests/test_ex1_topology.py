"""Pruebas de la EX1-Topology: composición, flavors, acceso a Internet,
reparto round robin y ciclo de vida completo."""

import unittest

from slice_orchestrator.driver_linux import DriverLinux
from slice_orchestrator.models import (
    CATALOGO_FLAVORS,
    EstadoSlice,
    Host,
    Nodo,
    TipoTopologia,
)
from slice_orchestrator.network_manager import VLANAllocator
from slice_orchestrator.orchestrator import (
    DespliegueFallidoError,
    desplegar_slice_existente,
    eliminar_slice,
)
from slice_orchestrator.topology_manager import (
    Injerto,
    calcular_plan_despliegue,
    construir_ex1_topology,
    construir_topologia,
)
from slice_orchestrator.vm_placement import EstrategiaPlacement, asignar_hosts


def tres_workers():
    """Inventario con el orden que espera el reparto round robin."""
    return [
        Host(f"worker{i}", f"worker{i}", f"sim://worker{i}", 16, 32768, "az-lab")
        for i in (1, 2, 3)
    ]


def claves(slc):
    """nodo_id -> 'VM1', 'VM2', ... en el orden del diagrama."""
    return {n.id: f"VM{i}" for i, n in enumerate(slc.nodos, 1)}


def pares(slc):
    """Conjunto de enlaces como pares de claves cortas, sin dirección."""
    c = claves(slc)
    return {frozenset((c[e.nodo_origen], c[e.nodo_destino])) for e in slc.enlaces}


class PruebasEstructura(unittest.TestCase):
    """La forma del slice debe coincidir con el diagrama de la EX1."""

    def setUp(self):
        self.slc = construir_ex1_topology("ex1", "demo", "u", "az-lab")

    def test_tipo_y_tamano(self):
        self.assertEqual(self.slc.tipo_topologia, TipoTopologia.MIXTA)
        self.assertEqual(len(self.slc.nodos), 6)
        self.assertEqual(len(self.slc.enlaces), 6)

    def test_enlaces_coinciden_con_el_diagrama(self):
        esperados = {
            frozenset(("VM1", "VM2")),   # anillo
            frozenset(("VM2", "VM3")),
            frozenset(("VM3", "VM4")),
            frozenset(("VM4", "VM1")),
            frozenset(("VM4", "VM5")),   # rama lineal injertada sobre VM4
            frozenset(("VM5", "VM6")),
        }
        self.assertEqual(pares(self.slc), esperados)

    def test_grado_de_cada_nodo(self):
        """VM4 es el nodo de unión: participa en el anillo y en la rama."""
        c = claves(self.slc)
        grado = {f"VM{i}": 0 for i in range(1, 7)}
        for enlace in self.slc.enlaces:
            grado[c[enlace.nodo_origen]] += 1
            grado[c[enlace.nodo_destino]] += 1
        self.assertEqual(grado["VM4"], 3)   # dos del anillo + uno de la rama
        self.assertEqual(grado["VM6"], 1)   # extremo de la rama
        for vm in ("VM1", "VM2", "VM3", "VM5"):
            self.assertEqual(grado[vm], 2, f"{vm} deberia tener dos enlaces")

    def test_el_grafo_es_conexo(self):
        c = claves(self.slc)
        vecinos = {f"VM{i}": set() for i in range(1, 7)}
        for enlace in self.slc.enlaces:
            a, b = c[enlace.nodo_origen], c[enlace.nodo_destino]
            vecinos[a].add(b)
            vecinos[b].add(a)
        visitados, pendientes = set(), ["VM1"]
        while pendientes:
            actual = pendientes.pop()
            if actual in visitados:
                continue
            visitados.add(actual)
            pendientes.extend(vecinos[actual] - visitados)
        self.assertEqual(len(visitados), 6)


class PruebasFlavorsEInternet(unittest.TestCase):
    def setUp(self):
        self.slc = construir_ex1_topology("ex1", "demo", "u", "az-lab")
        self.por_clave = {f"VM{i}": n for i, n in enumerate(self.slc.nodos, 1)}

    def test_flavor_por_nodo(self):
        esperado = {
            "VM1": "ubuntu", "VM2": "cirros", "VM3": "ubuntu",
            "VM4": "ubuntu", "VM5": "ubuntu", "VM6": "cirros",
        }
        obtenido = {k: n.flavor for k, n in self.por_clave.items()}
        self.assertEqual(obtenido, esperado)

    def test_recursos_tomados_del_catalogo(self):
        for clave, nodo in self.por_clave.items():
            f = CATALOGO_FLAVORS[nodo.flavor]
            self.assertEqual(nodo.vcpu, f.vcpu, clave)
            self.assertEqual(nodo.ram_mb, f.ram_mb, clave)
            self.assertEqual(nodo.disco_gb, f.disco_gb, clave)
            self.assertEqual(nodo.imagen, f.imagen, clave)

    def test_valores_de_la_especificacion(self):
        """Los dos flavors son los que fija el diagrama de la EX1."""
        ubuntu = CATALOGO_FLAVORS["ubuntu"]
        cirros = CATALOGO_FLAVORS["cirros"]
        self.assertEqual((ubuntu.vcpu, ubuntu.ram_mb, ubuntu.disco_gb), (1, 512, 2.2))
        self.assertEqual((cirros.vcpu, cirros.ram_mb, cirros.disco_gb), (1, 512, 1.0))

    def test_internet_solo_en_vm1_y_vm3(self):
        con_acceso = {k for k, n in self.por_clave.items() if n.acceso_internet}
        self.assertEqual(con_acceso, {"VM1", "VM3"})

    def test_flavor_desconocido_es_rechazado(self):
        with self.assertRaises(ValueError):
            construir_topologia("s", "s", "u", TipoTopologia.LINEAL, 3,
                                flavor_map={"vm1": "inexistente"})

    def test_nodo_desconocido_es_rechazado(self):
        with self.assertRaises(ValueError):
            construir_topologia("s", "s", "u", TipoTopologia.LINEAL, 3,
                                internet_access=["vm9"])


class PruebasComposicion(unittest.TestCase):
    def test_sin_injertos_el_tipo_no_cambia(self):
        slc = construir_topologia("s", "s", "u", TipoTopologia.ANILLO, 4)
        self.assertEqual(slc.tipo_topologia, TipoTopologia.ANILLO)

    def test_un_injerto_convierte_el_slice_en_mixto(self):
        slc = construir_topologia(
            "s", "s", "u", TipoTopologia.ANILLO, 4,
            injertos=[Injerto(sobre="vm1", tipo=TipoTopologia.LINEAL, num_nodos=3)],
        )
        self.assertEqual(slc.tipo_topologia, TipoTopologia.MIXTA)
        self.assertEqual(len(slc.nodos), 7)
        self.assertEqual(len(slc.enlaces), 4 + 3)

    def test_injerto_sobre_nodo_inexistente(self):
        with self.assertRaises(ValueError):
            construir_topologia(
                "s", "s", "u", TipoTopologia.ANILLO, 4,
                injertos=[Injerto(sobre="vm99", tipo=TipoTopologia.LINEAL, num_nodos=2)],
            )

    def test_los_ids_de_enlace_no_se_repiten(self):
        slc = construir_ex1_topology("ex1", "demo", "u")
        ids = [e.id for e in slc.enlaces]
        self.assertEqual(len(ids), len(set(ids)))


class PruebasRoundRobin(unittest.TestCase):
    def test_reparto_exacto_sobre_tres_workers(self):
        slc = construir_ex1_topology("ex1", "demo", "u", "az-lab")
        workers = tres_workers()
        asignacion = asignar_hosts(slc, workers, EstrategiaPlacement.ROUND_ROBIN)

        c = claves(slc)
        obtenido = {c[nid]: hid for nid, hid in asignacion.items()}
        esperado = {
            "VM1": "worker1", "VM4": "worker1",
            "VM2": "worker2", "VM5": "worker2",
            "VM3": "worker3", "VM6": "worker3",
        }
        self.assertEqual(obtenido, esperado)

    def test_la_estrategia_por_defecto_sigue_siendo_balanceada(self):
        slc = construir_ex1_topology("ex1", "demo", "u", "az-lab")
        workers = tres_workers()
        asignacion = asignar_hosts(slc, workers)
        self.assertEqual(len(asignacion), 6)

    def test_round_robin_falla_si_el_host_asignado_no_tiene_capacidad(self):
        slc = construir_ex1_topology("ex1", "demo", "u", "az-lab")
        workers = [
            Host("worker1", "worker1", "sim://w1", 16, 32768, "az-lab"),
            Host("worker2", "worker2", "sim://w2", 1, 256, "az-lab"),   # no cabe
            Host("worker3", "worker3", "sim://w3", 16, 32768, "az-lab"),
        ]
        with self.assertRaises(ValueError):
            asignar_hosts(slc, workers, EstrategiaPlacement.ROUND_ROBIN)


class PruebasPlanDeDespliegue(unittest.TestCase):
    def test_dos_olas_redes_y_luego_vms(self):
        slc = construir_ex1_topology("ex1", "demo", "u")
        plan = calcular_plan_despliegue(slc)

        self.assertEqual(len(plan["olas"]), 2)
        self.assertTrue(all(p["accion"] == "crear_red" for p in plan["olas"][0]))
        self.assertTrue(all(p["accion"] == "crear_vm" for p in plan["olas"][1]))
        self.assertEqual(len(plan["olas"][0]), 6)
        self.assertEqual(len(plan["olas"][1]), 6)

    def test_el_rollback_es_el_orden_inverso(self):
        slc = construir_ex1_topology("ex1", "demo", "u")
        plan = calcular_plan_despliegue(slc)
        self.assertEqual(plan["orden_rollback"], list(reversed(plan["orden_creacion"])))


class PruebasCicloDeVida(unittest.TestCase):
    def test_despliegue_y_eliminacion_completos(self):
        workers = tres_workers()
        driver = DriverLinux()
        vlans = VLANAllocator(3000, 3099)

        slc = construir_ex1_topology("ex1", "demo", "u", "az-lab")
        slc = desplegar_slice_existente(
            slc, workers, driver, vlans, EstrategiaPlacement.ROUND_ROBIN
        )

        self.assertEqual(slc.estado, EstadoSlice.ACTIVO)
        self.assertEqual(len(driver.vms_simuladas), 6)
        self.assertEqual(sum(h.vcpu_usados for h in workers), 6)
        self.assertTrue(all(n.host_id is not None for n in slc.nodos))

        eliminar_slice(slc, workers, driver, vlans)

        self.assertEqual(slc.estado, EstadoSlice.ELIMINADO)
        self.assertEqual(len(driver.vms_simuladas), 0)
        self.assertEqual(len(driver.redes_simuladas), 0)
        self.assertEqual(sum(h.vcpu_usados for h in workers), 0)
        self.assertEqual(sum(h.ram_usados_mb for h in workers), 0)

    def test_una_vlan_distinta_por_enlace(self):
        workers = tres_workers()
        driver = DriverLinux()
        vlans = VLANAllocator(3000, 3099)

        slc = construir_ex1_topology("ex1", "demo", "u", "az-lab")
        slc = desplegar_slice_existente(slc, workers, driver, vlans)

        asignadas = [e.vlan_id for e in slc.enlaces]
        self.assertEqual(len(asignadas), 6)
        self.assertEqual(len(set(asignadas)), 6)
        self.assertTrue(all(3000 <= v <= 3099 for v in asignadas))

    def test_los_enlaces_entre_workers_crean_la_red_en_ambos_extremos(self):
        workers = tres_workers()
        driver = DriverLinux()
        vlans = VLANAllocator(3000, 3099)

        slc = construir_ex1_topology("ex1", "demo", "u", "az-lab")
        slc = desplegar_slice_existente(
            slc, workers, driver, vlans, EstrategiaPlacement.ROUND_ROBIN
        )

        por_id = {n.id: n for n in slc.nodos}
        for enlace in slc.enlaces:
            hosts_del_enlace = {
                por_id[enlace.nodo_origen].host_id,
                por_id[enlace.nodo_destino].host_id,
            }
            creadas = {h for h, red in driver.redes_simuladas if red == enlace.nombre_red}
            self.assertEqual(
                {w.uri_libvirt for w in workers if w.id in hosts_del_enlace},
                creadas,
                f"La red del enlace {enlace.id} debe existir en ambos extremos",
            )

    def test_rollback_no_deja_recursos_huerfanos(self):
        workers = tres_workers()
        driver = DriverLinux(fallar_en_operacion=5)
        vlans = VLANAllocator(3000, 3099)

        slc = construir_ex1_topology("fallo", "demo", "u", "az-lab")
        with self.assertRaises(DespliegueFallidoError):
            desplegar_slice_existente(
                slc, workers, driver, vlans, EstrategiaPlacement.ROUND_ROBIN
            )

        self.assertEqual(slc.estado, EstadoSlice.ERROR)
        self.assertEqual(len(driver.vms_simuladas), 0)
        self.assertEqual(len(driver.redes_simuladas), 0)
        self.assertEqual(sum(h.vcpu_usados for h in workers), 0)
        self.assertNotIn("fallo", vlans._reservas)


class PruebasNodoDesdeFlavor(unittest.TestCase):
    def test_construye_con_los_valores_del_catalogo(self):
        nodo = Nodo.desde_flavor("n1", "vm1", "cirros", acceso_internet=True)
        self.assertEqual(nodo.flavor, "cirros")
        self.assertEqual(nodo.disco_gb, 1.0)
        self.assertEqual(nodo.imagen, "cirros-0.6.2.qcow2")
        self.assertTrue(nodo.acceso_internet)

    def test_flavor_desconocido(self):
        with self.assertRaises(ValueError):
            Nodo.desde_flavor("n1", "vm1", "no-existe")


if __name__ == "__main__":
    unittest.main()
