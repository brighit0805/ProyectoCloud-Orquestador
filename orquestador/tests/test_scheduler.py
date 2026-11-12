"""
test_scheduler.py
-----------------
Pruebas unitarias del scheduler inteligente (Lab 7).

Cubre: filtros, pesadores, overcommit, AZ obligatoria, afinidad topologica,
traza explicable, monitor de recursos, concurrencia y determinismo de la EX1.
"""

import sys
import os
import unittest
import time
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slice_orchestrator.models import (
    Slice, Nodo, Enlace, Host, EstadoSlice, TipoTopologia,
)
from slice_orchestrator.scheduler import (
    Scheduler, ConfigScheduler, ResourceMonitor, ResultadoScheduler,
    Filtro, FiltroZonaDisponibilidad, FiltroHostActivo,
    FiltroCapacidadVCPU, FiltroCapacidadRAM, FiltroCapacidadDisco,
    PesadorBalanceVCPU, PesadorBalanceRAM, PesadorAfinidadTopologica,
    PesadorAntiAfinidad,
    FILTROS_POR_DEFECTO, PERFIL_BALANCEADO, PERFIL_AFINIDAD_TOPOLOGICA,
    PERFIL_DISTRIBUIDO,
)
from slice_orchestrator.vm_placement import (
    EstrategiaPlacement, asignar_hosts, confirmar_asignacion,
    liberar_asignacion, obtener_ultimo_resultado,
)


def _hosts_3workers(vcpu=4, ram=3935):
    return [
        Host(id=f"worker{i}", nombre=f"worker{i}",
             uri_libvirt=f"qemu+ssh://w{i}/system",
             capacidad_vcpu=vcpu, capacidad_ram_mb=ram,
             zona_disponibilidad="az-default")
        for i in range(1, 4)
    ]


def _slice_simple(n_nodos=3, vcpu=1, ram=512, disco=2.0, zona="az-default"):
    nodos = [Nodo(id=f"n{i}", nombre=f"vm{i}", vcpu=vcpu, ram_mb=ram, disco_gb=disco)
             for i in range(1, n_nodos + 1)]
    enlaces = [Enlace(id=f"e{i}", nodo_origen=f"n{i}", nodo_destino=f"n{i+1}")
               for i in range(1, n_nodos)]
    return Slice(id="s1", nombre="test", owner="u1",
                 tipo_topologia=TipoTopologia.LINEAL,
                 nodos=nodos, enlaces=enlaces,
                 estado=EstadoSlice.CREANDO,
                 zona_disponibilidad=zona)


# ========================== FILTROS ==========================================

class TestFiltroZona(unittest.TestCase):
    def test_acepta_zona_correcta(self):
        f = FiltroZonaDisponibilidad()
        h = _hosts_3workers()[0]
        n = Nodo(id="n1", nombre="vm1")
        self.assertTrue(f.aceptar(h, n, {"zona_disponibilidad": "az-default"}))

    def test_rechaza_zona_incorrecta(self):
        f = FiltroZonaDisponibilidad()
        h = _hosts_3workers()[0]
        n = Nodo(id="n1", nombre="vm1")
        self.assertFalse(f.aceptar(h, n, {"zona_disponibilidad": "otra-zona"}))

    def test_acepta_sin_zona(self):
        f = FiltroZonaDisponibilidad()
        h = _hosts_3workers()[0]
        n = Nodo(id="n1", nombre="vm1")
        self.assertTrue(f.aceptar(h, n, {"zona_disponibilidad": None}))


class TestFiltroActivo(unittest.TestCase):
    def test_acepta_activo(self):
        f = FiltroHostActivo()
        h = _hosts_3workers()[0]
        self.assertTrue(f.aceptar(h, Nodo(id="n1", nombre="vm1"), {}))

    def test_rechaza_inactivo(self):
        f = FiltroHostActivo()
        h = _hosts_3workers()[0]
        h.activo = False
        self.assertFalse(f.aceptar(h, Nodo(id="n1", nombre="vm1"), {}))


class TestFiltroCapacidad(unittest.TestCase):
    def test_vcpu_suficientes(self):
        f = FiltroCapacidadVCPU()
        h = _hosts_3workers()[0]  # 4 vcpu libres
        n = Nodo(id="n1", nombre="vm1", vcpu=2)
        self.assertTrue(f.aceptar(h, n, {}))

    def test_vcpu_insuficientes(self):
        f = FiltroCapacidadVCPU()
        h = _hosts_3workers()[0]
        h.vcpu_usados = 4  # 0 libres
        n = Nodo(id="n1", nombre="vm1", vcpu=1)
        self.assertFalse(f.aceptar(h, n, {}))

    def test_vcpu_con_overcommit(self):
        """Con overcommit 4:1, un host de 4 vCPU acepta hasta 16."""
        f = FiltroCapacidadVCPU()
        h = _hosts_3workers()[0]
        h.vcpu_usados = 4  # sin overcommit estaria lleno
        h.overcommit_vcpu = 4.0
        n = Nodo(id="n1", nombre="vm1", vcpu=1)
        self.assertTrue(f.aceptar(h, n, {}))
        # Verificar que 16 vcpu es el limite
        h.vcpu_usados = 16
        self.assertFalse(f.aceptar(h, n, {}))

    def test_ram_suficiente(self):
        f = FiltroCapacidadRAM()
        h = _hosts_3workers()[0]
        n = Nodo(id="n1", nombre="vm1", ram_mb=512)
        self.assertTrue(f.aceptar(h, n, {}))

    def test_disco_no_informado_pasa(self):
        f = FiltroCapacidadDisco()
        h = _hosts_3workers()[0]  # capacidad_disco_gb=0
        n = Nodo(id="n1", nombre="vm1", disco_gb=100)
        self.assertTrue(f.aceptar(h, n, {}))

    def test_disco_insuficiente(self):
        f = FiltroCapacidadDisco()
        h = _hosts_3workers()[0]
        h.capacidad_disco_gb = 50.0
        h.disco_usado_gb = 49.0
        n = Nodo(id="n1", nombre="vm1", disco_gb=5.0)
        self.assertFalse(f.aceptar(h, n, {}))


# ========================== PESADORES ========================================

class TestPesadorBalance(unittest.TestCase):
    def test_mas_libre_puntua_mas(self):
        p = PesadorBalanceVCPU()
        h1 = _hosts_3workers()[0]
        h1.vcpu_usados = 0  # 100% libre
        h2 = _hosts_3workers()[1]
        h2.vcpu_usados = 3  # 25% libre
        n = Nodo(id="n1", nombre="vm1")
        self.assertGreater(
            p.puntuar(h1, n, {}),
            p.puntuar(h2, n, {}),
        )

    def test_ram_mas_libre_puntua_mas(self):
        p = PesadorBalanceRAM()
        h1 = _hosts_3workers()[0]
        h1.ram_usados_mb = 0
        h2 = _hosts_3workers()[1]
        h2.ram_usados_mb = 3000
        n = Nodo(id="n1", nombre="vm1")
        self.assertGreater(
            p.puntuar(h1, n, {}),
            p.puntuar(h2, n, {}),
        )


class TestPesadorAfinidad(unittest.TestCase):
    def test_vecino_en_mismo_host_puntua_mas(self):
        p = PesadorAfinidadTopologica()
        slc = _slice_simple(3)
        # n1 esta en worker1
        ctx = {"slice": slc, "asignacion_parcial": {"n1": "worker1"}}
        n2 = slc.nodos[1]  # n2 es vecino de n1 y n3
        h1 = _hosts_3workers()[0]  # worker1 (tiene al vecino n1)
        h2 = _hosts_3workers()[1]  # worker2 (no tiene vecinos)
        self.assertGreater(
            p.puntuar(h1, n2, ctx),
            p.puntuar(h2, n2, ctx),
        )

    def test_sin_vecinos_asignados(self):
        p = PesadorAfinidadTopologica()
        slc = _slice_simple(3)
        ctx = {"slice": slc, "asignacion_parcial": {}}
        n1 = slc.nodos[0]
        h1 = _hosts_3workers()[0]
        # Sin vecinos asignados, puntaje 0
        self.assertEqual(p.puntuar(h1, n1, ctx), 0.0)


class TestPesadorAntiAfinidad(unittest.TestCase):
    def test_host_vacio_puntua_mas(self):
        p = PesadorAntiAfinidad()
        ctx = {"asignacion_parcial": {"n1": "worker1"}, "total_hosts": 3}
        n = Nodo(id="n2", nombre="vm2")
        h1 = _hosts_3workers()[0]  # worker1 (ya tiene 1 VM)
        h2 = _hosts_3workers()[1]  # worker2 (vacio)
        self.assertGreater(
            p.puntuar(h2, n, ctx),
            p.puntuar(h1, n, ctx),
        )


# ========================== SCHEDULER COMPLETO ===============================

class TestSchedulerBasico(unittest.TestCase):
    def test_asigna_todos_los_nodos(self):
        slc = _slice_simple(3)
        hosts = _hosts_3workers()
        sched = Scheduler(config=ConfigScheduler(monitorear=False))
        resultado = sched.programar(slc, hosts)
        self.assertEqual(len(resultado.asignacion), 3)
        for nodo in slc.nodos:
            self.assertIn(nodo.id, resultado.asignacion)

    def test_traza_explicable(self):
        slc = _slice_simple(3)
        hosts = _hosts_3workers()
        sched = Scheduler(config=ConfigScheduler(monitorear=False))
        resultado = sched.programar(slc, hosts)
        texto = resultado.explicacion_texto()
        self.assertIn("Decisiones del scheduler", texto)
        self.assertIn("Elegido:", texto)
        # Cada nodo tiene una decision
        self.assertEqual(len(resultado.decisiones), 3)
        for d in resultado.decisiones:
            self.assertIsNotNone(d.host_elegido)
            self.assertTrue(len(d.candidatos) > 0)

    def test_falla_sin_capacidad(self):
        slc = _slice_simple(3, vcpu=10)  # 10 vcpu por nodo, hosts tienen 4
        hosts = _hosts_3workers()
        sched = Scheduler(config=ConfigScheduler(monitorear=False))
        with self.assertRaises(ValueError):
            sched.programar(slc, hosts)

    def test_zona_obligatoria(self):
        """Solo los hosts de la zona del slice son candidatos."""
        slc = _slice_simple(2, zona="az-prod")
        hosts = _hosts_3workers()
        hosts[0].zona_disponibilidad = "az-prod"
        # hosts[1] y hosts[2] quedan en az-default
        sched = Scheduler(config=ConfigScheduler(monitorear=False))
        resultado = sched.programar(slc, hosts)
        # Todos los nodos deben ir al worker1 (unico en az-prod)
        for host_id in resultado.asignacion.values():
            self.assertEqual(host_id, "worker1")

    def test_host_inactivo_descartado(self):
        slc = _slice_simple(2)
        hosts = _hosts_3workers()
        hosts[0].activo = False
        hosts[1].activo = False
        # Solo queda worker3
        sched = Scheduler(config=ConfigScheduler(monitorear=False))
        resultado = sched.programar(slc, hosts)
        for host_id in resultado.asignacion.values():
            self.assertEqual(host_id, "worker3")


class TestSchedulerOvercommit(unittest.TestCase):
    def test_overcommit_permite_mas_vms(self):
        """Con overcommit 4:1, caben 16 VMs de 1 vCPU en un host de 4 vCPU."""
        slc = _slice_simple(12, vcpu=1, ram=100)
        hosts = [Host(id="w1", nombre="w1", uri_libvirt="qemu://w1",
                      capacidad_vcpu=4, capacidad_ram_mb=4000,
                      zona_disponibilidad="az-default")]
        config = ConfigScheduler(
            overcommit_vcpu=4.0,
            overcommit_ram=1.0,
            monitorear=False,
        )
        sched = Scheduler(config=config)
        resultado = sched.programar(slc, hosts)
        self.assertEqual(len(resultado.asignacion), 12)

    def test_sin_overcommit_falla_por_capacidad(self):
        """Sin overcommit, 5 VMs de 1 vCPU no caben en 1 host de 4 vCPU."""
        slc = _slice_simple(5, vcpu=1, ram=100)
        hosts = [Host(id="w1", nombre="w1", uri_libvirt="qemu://w1",
                      capacidad_vcpu=4, capacidad_ram_mb=4000,
                      zona_disponibilidad="az-default")]
        config = ConfigScheduler(overcommit_vcpu=1.0, monitorear=False)
        sched = Scheduler(config=config)
        with self.assertRaises(ValueError):
            sched.programar(slc, hosts)


class TestSchedulerAfinidadTopologica(unittest.TestCase):
    def test_vecinos_prefieren_mismo_host(self):
        """En una cadena lineal n1-n2-n3, n2 deberia preferir el host de n1."""
        slc = _slice_simple(3)
        hosts = _hosts_3workers()
        config = ConfigScheduler(
            pesadores=[
                PesadorAfinidadTopologica(peso=10.0),  # peso muy alto
                PesadorBalanceVCPU(peso=0.1),
            ],
            monitorear=False,
        )
        sched = Scheduler(config=config)
        resultado = sched.programar(slc, hosts)
        # n1 va a algun host; n2 (vecino de n1) deberia ir al mismo
        host_n1 = resultado.asignacion["n1"]
        host_n2 = resultado.asignacion["n2"]
        self.assertEqual(host_n1, host_n2,
                         "Con afinidad alta, n2 deberia ir al mismo host que n1")

    def test_anti_afinidad_distribuye(self):
        """Con anti-afinidad alta, los nodos se distribuyen entre hosts."""
        slc = _slice_simple(3)
        hosts = _hosts_3workers()
        config = ConfigScheduler(
            pesadores=[
                PesadorAntiAfinidad(peso=10.0),
                PesadorBalanceVCPU(peso=0.1),
            ],
            monitorear=False,
        )
        sched = Scheduler(config=config)
        resultado = sched.programar(slc, hosts)
        hosts_usados = set(resultado.asignacion.values())
        self.assertEqual(len(hosts_usados), 3,
                         "Con anti-afinidad alta, cada nodo deberia ir a un host distinto")


# ========================== MONITOR ==========================================

class _DriverFalso:
    """Driver simulado que reporta uso de recursos."""
    def __init__(self, hosts: list[Host]):
        self._hosts = hosts

    def descubrir_hosts(self) -> list[Host]:
        return self._hosts


class TestResourceMonitor(unittest.TestCase):
    def test_refresca_uso(self):
        hosts = _hosts_3workers()
        hosts_reales = _hosts_3workers()
        hosts_reales[0].vcpu_usados = 2
        hosts_reales[0].ram_usados_mb = 1000
        driver = _DriverFalso(hosts_reales)
        monitor = ResourceMonitor(driver=driver, staleness_s=0)
        monitor.refrescar(hosts, forzar=True)
        self.assertEqual(hosts[0].vcpu_usados, 2)
        self.assertEqual(hosts[0].ram_usados_mb, 1000)
        self.assertIsNotNone(hosts[0].ultima_medicion)

    def test_refresca_disco_real(self):
        """Lab 7: el disco real tambien debe propagarse en cada refresco, no
        solo vCPU/RAM (antes quedaba desactualizado y el filtro de disco
        nunca veia datos reales)."""
        hosts = _hosts_3workers()
        hosts_reales = _hosts_3workers()
        hosts_reales[0].capacidad_disco_gb = 9.0
        hosts_reales[0].disco_usado_gb = 8.0
        driver = _DriverFalso(hosts_reales)
        monitor = ResourceMonitor(driver=driver, staleness_s=0)
        monitor.refrescar(hosts, forzar=True)
        self.assertEqual((hosts[0].capacidad_disco_gb, hosts[0].disco_usado_gb), (9.0, 8.0))

    def test_no_refresca_sin_driver(self):
        hosts = _hosts_3workers()
        monitor = ResourceMonitor(driver=None)
        monitor.refrescar(hosts)
        self.assertEqual(hosts[0].vcpu_usados, 0)  # sin cambios

    def test_staleness_evita_refresco(self):
        hosts = _hosts_3workers()
        hosts_reales = _hosts_3workers()
        hosts_reales[0].vcpu_usados = 2
        driver = _DriverFalso(hosts_reales)
        monitor = ResourceMonitor(driver=driver, staleness_s=9999)
        # Primer refresco funciona
        monitor.refrescar(hosts, forzar=True)
        self.assertEqual(hosts[0].vcpu_usados, 2)
        # Resetear y ver que no refresca de nuevo
        hosts[0].vcpu_usados = 0
        hosts_reales[0].vcpu_usados = 3
        monitor.refrescar(hosts)  # no forzado, dentro de staleness
        self.assertEqual(hosts[0].vcpu_usados, 0)  # no cambio


# ========================== FACHADA vm_placement =============================

class TestFachadaObjetivo(unittest.TestCase):
    def test_estrategia_objetivo_devuelve_asignacion(self):
        slc = _slice_simple(3)
        hosts = _hosts_3workers()
        asignacion = asignar_hosts(
            slc, hosts, EstrategiaPlacement.OBJETIVO,
            overcommit_vcpu=1.0, overcommit_ram=1.0,
        )
        self.assertEqual(len(asignacion), 3)
        resultado = obtener_ultimo_resultado()
        self.assertIsNotNone(resultado)
        self.assertEqual(len(resultado.decisiones), 3)

    def test_round_robin_sigue_funcionando(self):
        """La EX1 necesita round robin: vm1->w1, vm2->w2, vm3->w3."""
        slc = _slice_simple(6)
        hosts = _hosts_3workers()
        asignacion = asignar_hosts(slc, hosts, EstrategiaPlacement.ROUND_ROBIN)
        self.assertEqual(asignacion["n1"], "worker1")
        self.assertEqual(asignacion["n2"], "worker2")
        self.assertEqual(asignacion["n3"], "worker3")
        self.assertEqual(asignacion["n4"], "worker1")
        self.assertEqual(asignacion["n5"], "worker2")
        self.assertEqual(asignacion["n6"], "worker3")

    def test_balanceado_sigue_funcionando(self):
        slc = _slice_simple(3)
        hosts = _hosts_3workers()
        hosts[0].vcpu_usados = 3  # worker1 casi lleno
        asignacion = asignar_hosts(slc, hosts, EstrategiaPlacement.BALANCEADO)
        # worker1 no deberia recibir la primera VM (tiene menos capacidad)
        self.assertNotEqual(asignacion["n1"], "worker1")

    def test_objetivo_con_overcommit(self):
        slc = _slice_simple(8, vcpu=1, ram=100)
        hosts = [Host(id="w1", nombre="w1", uri_libvirt="q://w1",
                      capacidad_vcpu=4, capacidad_ram_mb=4000,
                      zona_disponibilidad="az-default")]
        asignacion = asignar_hosts(
            slc, hosts, EstrategiaPlacement.OBJETIVO,
            overcommit_vcpu=4.0,
        )
        self.assertEqual(len(asignacion), 8)


class TestFachadaMonitorReal(unittest.TestCase):
    def test_objetivo_con_driver_simulado(self):
        """El scheduler puede usar un driver para leer estado real."""
        hosts = _hosts_3workers()
        hosts_reales = _hosts_3workers()
        hosts_reales[0].vcpu_usados = 3  # worker1 casi lleno (medido por Nova)
        driver = _DriverFalso(hosts_reales)

        slc = _slice_simple(3)
        config = ConfigScheduler(monitorear=True, staleness_s=0)
        asignacion = asignar_hosts(
            slc, hosts, EstrategiaPlacement.OBJETIVO,
            driver=driver, config_scheduler=config,
        )
        # El scheduler deberia evitar worker1 para el primer nodo
        self.assertNotEqual(asignacion["n1"], "worker1")


# ========================== CONCURRENCIA =====================================

class TestConcurrencia(unittest.TestCase):
    def test_programaciones_paralelas_no_interfieren(self):
        """Dos schedulers independientes no se pisan."""
        resultados = {}
        errores = []

        def programar(nombre, slc, hosts):
            try:
                sched = Scheduler(config=ConfigScheduler(monitorear=False))
                r = sched.programar(slc, hosts)
                resultados[nombre] = r.asignacion
            except Exception as e:
                errores.append(e)

        hilos = []
        for i in range(5):
            slc = _slice_simple(3)
            slc.id = f"s{i}"
            hosts = _hosts_3workers()
            t = threading.Thread(target=programar, args=(f"t{i}", slc, hosts))
            hilos.append(t)
            t.start()

        for t in hilos:
            t.join(timeout=10)

        self.assertEqual(len(errores), 0, f"Errores: {errores}")
        self.assertEqual(len(resultados), 5)


# ========================== DISCO ============================================

class TestFiltroDisco(unittest.TestCase):
    def test_disco_informado_y_suficiente(self):
        slc = _slice_simple(1, disco=5.0)
        hosts = [Host(id="w1", nombre="w1", uri_libvirt="q://w1",
                      capacidad_vcpu=4, capacidad_ram_mb=4000,
                      capacidad_disco_gb=100.0,
                      zona_disponibilidad="az-default")]
        sched = Scheduler(config=ConfigScheduler(monitorear=False))
        resultado = sched.programar(slc, hosts)
        self.assertEqual(len(resultado.asignacion), 1)

    def test_disco_insuficiente_rechazado(self):
        slc = _slice_simple(1, disco=50.0)
        hosts = [Host(id="w1", nombre="w1", uri_libvirt="q://w1",
                      capacidad_vcpu=4, capacidad_ram_mb=4000,
                      capacidad_disco_gb=10.0,
                      zona_disponibilidad="az-default")]
        sched = Scheduler(config=ConfigScheduler(monitorear=False))
        with self.assertRaises(ValueError):
            sched.programar(slc, hosts)


if __name__ == "__main__":
    unittest.main()
