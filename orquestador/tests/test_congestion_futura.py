"""
test_congestion_futura.py
-------------------------
Pruebas de los dos criterios de R4-Diseno que el scheduler no cubria:

  1. Estimado de congestion futura (no solo la utilizacion actual):
       - ModeloCongestion / FiltroCongestionFutura (placement estocastico,
         demanda prevista mu + z*sigma contra la capacidad fisica).
       - RegistroReservas (asignaciones recientes que el driver aun no mide).
  2. Limite de tiempo de ejecucion para slices grandes (modo rapido).
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slice_orchestrator.models import (
    Slice, Nodo, Enlace, Host, EstadoSlice, TipoTopologia,
)
from slice_orchestrator.scheduler import (
    Scheduler, ConfigScheduler, ResourceMonitor, ModeloCongestion,
    FiltroCongestionFutura, RegistroReservas, REGISTRO_RESERVAS,
)
from slice_orchestrator.vm_placement import (
    EstrategiaPlacement, asignar_hosts, confirmar_asignacion, liberar_asignacion,
)


def _host(id_, vcpu=4, ram=4096, zona="az-default"):
    return Host(id=id_, nombre=id_, uri_libvirt=f"qemu+ssh://{id_}/system",
                capacidad_vcpu=vcpu, capacidad_ram_mb=ram,
                zona_disponibilidad=zona)


def _slice(n, vcpu=1, ram=512, disco=1.0, id_="s1", malla=False):
    nodos = [Nodo(id=f"n{i}", nombre=f"vm{i}", vcpu=vcpu, ram_mb=ram, disco_gb=disco)
             for i in range(1, n + 1)]
    if malla:
        pares = [(i, j) for i in range(1, n + 1) for j in range(i + 1, n + 1)]
    else:
        pares = [(i, i + 1) for i in range(1, n)]
    enlaces = [Enlace(id=f"e{k}", nodo_origen=f"n{i}", nodo_destino=f"n{j}")
               for k, (i, j) in enumerate(pares)]
    return Slice(id=id_, nombre=id_, owner="u1",
                 tipo_topologia=TipoTopologia.MALLA if malla else TipoTopologia.LINEAL,
                 nodos=nodos, enlaces=enlaces, estado=EstadoSlice.CREANDO,
                 zona_disponibilidad="az-default")


class _Reloj:
    """Reloj controlable para pruebas (reemplaza time.monotonic)."""
    def __init__(self, t=1000.0, paso=0.0):
        self.t = t
        self.paso = paso

    def __call__(self):
        self.t += self.paso
        return self.t


class _DriverFijo:
    """Driver que siempre informa el mismo uso (no 've' las VMs nuevas)."""
    def __init__(self, hosts):
        self.hosts = hosts

    def descubrir_hosts(self):
        return [Host(**{**h.__dict__}) for h in self.hosts]


# ===================== MODELO ESTOCASTICO =====================================

class TestModeloCongestion(unittest.TestCase):
    def test_z_del_sla(self):
        self.assertAlmostEqual(ModeloCongestion(prob_desborde=0.05).z, 1.6449, places=3)
        self.assertAlmostEqual(ModeloCongestion(prob_desborde=0.01).z, 2.3263, places=3)

    def test_prevista_es_mu_mas_z_sigma(self):
        m = ModeloCongestion(prob_desborde=0.05, media_vcpu=0.3, desv_vcpu=0.2)
        # 9 vCPU: mu = 2.7, sigma = 0.2 * 3 = 0.6 -> 2.7 + 1.645 * 0.6
        self.assertAlmostEqual(m.vcpu_prevista(9), 2.7 + m.z * 0.6, places=6)

    def test_prevista_nunca_supera_lo_asignado(self):
        m = ModeloCongestion(media_vcpu=0.9, desv_vcpu=0.5, media_ram=0.9, desv_ram=0.5)
        for a in range(0, 40):
            self.assertLessEqual(m.vcpu_prevista(a), a + 1e-9)
            self.assertLessEqual(m.ram_prevista_mb(a * 512), a * 512 + 1e-6)

    def test_sin_overcommit_nunca_rechaza(self):
        """1:1 -> lo asignado no supera la capacidad, la demanda prevista tampoco."""
        m = ModeloCongestion(media_vcpu=0.95, desv_vcpu=0.5)
        f = FiltroCongestionFutura(m)
        for usados in range(0, 4):
            h = _host("w1", vcpu=4, ram=4096)
            h.vcpu_usados = usados
            h.ram_usados_mb = usados * 1024
            self.assertTrue(f.aceptar(h, Nodo(id="n", nombre="n", vcpu=1, ram_mb=1024), {}))

    def test_estadistica_multiplexacion(self):
        """Mas unidades -> menor demanda prevista por unidad (LLN/CLT)."""
        m = ModeloCongestion()
        self.assertLess(m.vcpu_prevista(64) / 64, m.vcpu_prevista(4) / 4)

    def test_mas_tolerancia_al_riesgo_admite_mas(self):
        estricto = ModeloCongestion(prob_desborde=0.01)
        laxo = ModeloCongestion(prob_desborde=0.20)
        self.assertGreater(estricto.vcpu_prevista(10), laxo.vcpu_prevista(10))

    def test_validaciones(self):
        with self.assertRaises(ValueError):
            ModeloCongestion(prob_desborde=0.0)
        with self.assertRaises(ValueError):
            ModeloCongestion(prob_desborde=1.0)
        with self.assertRaises(ValueError):
            ModeloCongestion(media_vcpu=-0.1)
        with self.assertRaises(ValueError):
            ModeloCongestion(umbral=0)

    def test_desde_entorno(self):
        env = {"ORQ_CONGESTION_PROB_DESBORDE": "0.01",
               "ORQ_CONGESTION_MEDIA_VCPU": "0.5",
               "ORQ_CONGESTION_UMBRAL": "0.9"}
        with mock.patch.dict(os.environ, env):
            m = ModeloCongestion.desde_entorno()
        self.assertEqual((m.prob_desborde, m.media_vcpu, m.umbral), (0.01, 0.5, 0.9))
        self.assertEqual(m.desv_vcpu, ModeloCongestion().desv_vcpu)

    def test_entorno_invalido_usa_defecto(self):
        with mock.patch.dict(os.environ, {"ORQ_CONGESTION_MEDIA_VCPU": "mucho"}):
            m = ModeloCongestion.desde_entorno()
        self.assertEqual(m.media_vcpu, ModeloCongestion().media_vcpu)


class TestSchedulerCongestion(unittest.TestCase):
    def _config(self, modelo, overcommit=4.0):
        return ConfigScheduler(overcommit_vcpu=overcommit, overcommit_ram=1.0,
                               monitorear=False, modelo_congestion=modelo,
                               tiempo_limite_s=0)

    def test_overcommit_4_a_1_limitado_por_sla(self):
        """4:1 permitiria 16 VMs de 1 vCPU en un host de 4 cores; con media 30 %
        y P(desborde) <= 5 % el SLA solo admite 9 (0.3*9 + 1.645*0.2*3 = 3.69 <= 4,
        con 10 ya da 4.04 > 4)."""
        hosts = [_host("w1", vcpu=4, ram=8192)]
        modelo = ModeloCongestion(prob_desborde=0.05, media_vcpu=0.3, desv_vcpu=0.2)
        sched = Scheduler(config=self._config(modelo))
        self.assertEqual(len(sched.programar(_slice(9, ram=100), hosts).asignacion), 9)
        with self.assertRaises(ValueError) as ctx:
            Scheduler(config=self._config(modelo)).programar(_slice(10, ram=100), hosts)
        self.assertIn("congestion_futura", str(ctx.exception))

    def test_sin_modelo_vale_solo_el_overcommit(self):
        hosts = [_host("w1", vcpu=4, ram=8192)]
        sched = Scheduler(config=self._config(False))
        self.assertEqual(len(sched.programar(_slice(16, ram=100), hosts).asignacion), 16)

    def test_reparte_hacia_el_host_menos_congestionado(self):
        """Un host ya cargado deja de aceptar antes que uno vacio."""
        cargado, vacio = _host("cargado", vcpu=4, ram=8192), _host("vacio", vcpu=4, ram=8192)
        cargado.vcpu_usados = 9
        sched = Scheduler(config=self._config(ModeloCongestion()))
        res = sched.programar(_slice(3, ram=100), [cargado, vacio])
        self.assertEqual(set(res.asignacion.values()), {"vacio"})
        rechazos = [c.filtros_rechazados for d in res.decisiones for c in d.candidatos
                    if c.host_id == "cargado"]
        self.assertTrue(all("congestion_futura" in r for r in rechazos))

    def test_traza_muestra_uso_previsto(self):
        sched = Scheduler(config=self._config(ModeloCongestion()))
        res = sched.programar(_slice(2), [_host("w1")])
        self.assertIn("uso previsto al 95%", res.decisiones[0].razon)

    def test_filtro_por_defecto_activo(self):
        nombres = [f.nombre for f in ConfigScheduler().filtros]
        self.assertIn("congestion_futura", nombres)
        self.assertIsNotNone(ConfigScheduler().modelo_congestion)


# ===================== RESERVAS RECIENTES =====================================

class TestRegistroReservas(unittest.TestCase):
    def setUp(self):
        self.reloj = _Reloj()
        self.reg = RegistroReservas(ttl_s=100, reloj=self.reloj)

    def test_reserva_no_medida_se_suma(self):
        self.reg.aplicar_medicion("w1", 2, 1024, 0)
        self.reg.reservar("s/n1", "w1", 1, 512, 2.0)
        self.assertEqual(self.reg.aplicar_medicion("w1", 2, 1024, 0), (1, 512, 2.0))

    def test_reserva_reflejada_se_descarta(self):
        self.reg.aplicar_medicion("w1", 2, 1024, 0)
        self.reg.reservar("s/n1", "w1", 1, 512, 2.0)
        self.assertEqual(self.reg.aplicar_medicion("w1", 3, 1536, 0), (0, 0, 0))
        self.assertEqual(self.reg.reservas_de("w1"), [])

    def test_reflejo_parcial_conserva_la_pendiente(self):
        self.reg.aplicar_medicion("w1", 0, 0, 0)
        self.reg.reservar("s/n1", "w1", 1, 512, 0)
        self.reg.reservar("s/n2", "w1", 1, 512, 0)
        # El driver solo ve una de las dos VMs: queda pendiente una.
        self.assertEqual(self.reg.aplicar_medicion("w1", 1, 512, 0), (1, 512, 0))
        # Luego ve las dos.
        self.assertEqual(self.reg.aplicar_medicion("w1", 2, 1024, 0), (0, 0, 0))

    def test_vence_por_ttl(self):
        self.reg.reservar("s/n1", "w1", 1, 512, 0)
        self.reloj.t += 101
        self.assertEqual(self.reg.aplicar_medicion("w1", 0, 0, 0), (0, 0, 0))

    def test_sin_medicion_previa_espera_al_ttl(self):
        """Sin base no se puede saber si ya se refleja: se cuenta hasta el TTL."""
        self.reg.reservar("s/n1", "w1", 1, 512, 0)
        self.assertEqual(self.reg.aplicar_medicion("w1", 5, 5000, 0), (1, 512, 0))

    def test_liberar(self):
        self.reg.reservar("s/n1", "w1", 1, 512, 0)
        self.reg.liberar(["s/n1"])
        self.assertEqual(self.reg.aplicar_medicion("w1", 0, 0, 0), (0, 0, 0))

    def test_otro_host_no_se_afecta(self):
        self.reg.reservar("s/n1", "w1", 1, 512, 0)
        self.assertEqual(self.reg.aplicar_medicion("w2", 0, 0, 0), (0, 0, 0))

    def test_ttl_desde_entorno(self):
        with mock.patch.dict(os.environ, {"ORQ_SCHEDULER_TTL_RESERVA": "30"}):
            self.assertEqual(RegistroReservas().ttl_s, 30.0)


class TestMonitorConReservas(unittest.TestCase):
    def setUp(self):
        REGISTRO_RESERVAS.limpiar()

    def tearDown(self):
        REGISTRO_RESERVAS.limpiar()

    def test_monitor_suma_reservas_pendientes(self):
        reg = RegistroReservas(ttl_s=100, reloj=_Reloj())
        reales = [_host("w1")]
        monitor = ResourceMonitor(driver=_DriverFijo(reales), reservas=reg)
        hosts = [_host("w1")]
        monitor.refrescar(hosts, forzar=True)
        reg.reservar("s/n1", "w1", 2, 1024, 3.0)
        monitor.refrescar(hosts, forzar=True)
        self.assertEqual((hosts[0].vcpu_usados, hosts[0].ram_usados_mb,
                          hosts[0].disco_usado_gb), (2, 1024, 3.0))

    def test_slice_recien_confirmado_no_se_ignora(self):
        """Caso de la seccion 13.6: el slice A se confirma pero el driver aun no
        muestra sus VMs. Al programar el slice B, el monitor debe contarlas."""
        reales = [_host("w1", vcpu=2, ram=4096), _host("w2", vcpu=2, ram=4096)]
        driver = _DriverFijo(reales)        # nunca ve las VMs nuevas
        inventario = [_host("w1", vcpu=2, ram=4096), _host("w2", vcpu=2, ram=4096)]

        a = _slice(2, id_="A")
        asig_a = asignar_hosts(a, inventario, EstrategiaPlacement.OBJETIVO, driver=driver)
        confirmar_asignacion(a, inventario, asig_a)

        # Sin las reservas, B veria 4 vCPU libres y cabria; con ellas solo quedan 2.
        b = _slice(3, id_="B")
        with self.assertRaises(ValueError):
            asignar_hosts(b, inventario, EstrategiaPlacement.OBJETIVO, driver=driver)
        c = _slice(2, id_="C")
        asig_c = asignar_hosts(c, inventario, EstrategiaPlacement.OBJETIVO, driver=driver)
        usados_a = {h: list(asig_a.values()).count(h) for h in ("w1", "w2")}
        for h in asig_c.values():
            self.assertLess(usados_a[h], 2)

    def test_liberar_asignacion_quita_reservas(self):
        reales = [_host("w1", vcpu=2, ram=4096)]
        driver = _DriverFijo(reales)
        inventario = [_host("w1", vcpu=2, ram=4096)]
        a = _slice(2, id_="A")
        confirmar_asignacion(a, inventario,
                             asignar_hosts(a, inventario, EstrategiaPlacement.OBJETIVO,
                                           driver=driver))
        self.assertEqual(len(REGISTRO_RESERVAS.reservas_de("w1")), 2)
        liberar_asignacion(a, inventario)       # rollback o eliminacion
        self.assertEqual(REGISTRO_RESERVAS.reservas_de("w1"), [])
        b = _slice(2, id_="B")
        self.assertEqual(len(asignar_hosts(b, inventario, EstrategiaPlacement.OBJETIVO,
                                           driver=driver)), 2)


# ===================== LIMITE DE TIEMPO =======================================

class TestLimiteDeTiempo(unittest.TestCase):
    def _sched(self, limite, paso):
        config = ConfigScheduler(monitorear=False, tiempo_limite_s=limite,
                                 modelo_congestion=False)
        return Scheduler(config=config, reloj=_Reloj(paso=paso))

    def test_sin_superar_el_limite_no_hay_modo_rapido(self):
        res = self._sched(limite=100, paso=0.01).programar(_slice(5), [_host("w1"), _host("w2")])
        self.assertFalse(res.limite_de_tiempo_alcanzado)
        self.assertNotIn("limite de tiempo", res.explicacion_texto())

    def test_superado_el_limite_el_resto_va_en_modo_rapido(self):
        # Cada lectura del reloj avanza 1 s; con limite 2.5 s el corte llega
        # al evaluar el tercer nodo.
        res = self._sched(limite=2.5, paso=1.0).programar(
            _slice(8), [_host("w1", vcpu=8), _host("w2", vcpu=8)])
        self.assertEqual(len(res.asignacion), 8)
        self.assertEqual(res.nodos_modo_rapido, [f"n{i}" for i in range(3, 9)])
        self.assertIn("limite de tiempo", res.explicacion_texto())
        rapida = res.decisiones[-1]
        self.assertTrue(rapida.razon.startswith("Modo rapido"))
        self.assertTrue(all(c.puntajes == {} for c in rapida.candidatos))

    def test_modo_rapido_respeta_los_filtros(self):
        hosts = [_host("w1", vcpu=2), _host("w2", vcpu=2, zona="otra"), _host("w3", vcpu=2)]
        res = self._sched(limite=0.5, paso=1.0).programar(_slice(4), hosts)
        self.assertEqual(len(res.nodos_modo_rapido), 4)
        self.assertNotIn("w2", res.asignacion.values())
        for h in ("w1", "w3"):
            self.assertLessEqual(list(res.asignacion.values()).count(h), 2)
        with self.assertRaises(ValueError):
            self._sched(limite=0.5, paso=1.0).programar(_slice(5), hosts)

    def test_modo_rapido_reparte(self):
        res = self._sched(limite=0.5, paso=1.0).programar(
            _slice(6), [_host("w1"), _host("w2"), _host("w3")])
        self.assertEqual(sorted(list(res.asignacion.values()).count(h)
                                for h in ("w1", "w2", "w3")), [2, 2, 2])

    def test_limite_cero_desactiva(self):
        self.assertIsNone(ConfigScheduler(tiempo_limite_s=0).tiempo_limite_s)
        res = self._sched(limite=0, paso=100.0).programar(_slice(4), [_host("w1")])
        self.assertFalse(res.limite_de_tiempo_alcanzado)

    def test_limite_desde_entorno(self):
        with mock.patch.dict(os.environ, {"ORQ_SCHEDULER_TIEMPO_LIMITE": "2"}):
            self.assertEqual(ConfigScheduler().tiempo_limite_s, 2.0)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORQ_SCHEDULER_TIEMPO_LIMITE", None)
            self.assertEqual(ConfigScheduler().tiempo_limite_s, 5.0)

    def test_registra_duracion(self):
        res = self._sched(limite=100, paso=0.25).programar(_slice(2), [_host("w1")])
        self.assertGreater(res.duracion_s, 0)

    def test_malla_grande_termina_rapido(self):
        """Malla de 120 nodos (7140 enlaces) en 6 hosts con el reloj real:
        con los vecinos precalculados el calculo completo cabe en el limite."""
        hosts = [_host(f"w{i}", vcpu=64, ram=65536) for i in range(6)]
        config = ConfigScheduler(monitorear=False, tiempo_limite_s=5.0)
        res = Scheduler(config=config).programar(_slice(120, malla=True), hosts)
        self.assertEqual(len(res.asignacion), 120)
        self.assertFalse(res.limite_de_tiempo_alcanzado)
        self.assertLess(res.duracion_s, 5.0)


if __name__ == "__main__":
    unittest.main()
