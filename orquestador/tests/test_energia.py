"""
test_energia.py
---------------
R4 (energía): objetivo "energia" del scheduler, que consolida VMs en pocos
hosts (PesadorEnergia + PERFIL_ENERGIA), elegido por configuración.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slice_orchestrator.models import EstadoSlice, Host, Nodo, Slice, Enlace, TipoTopologia
from slice_orchestrator.scheduler import (
    ConfigScheduler, PesadorEnergia, PERFIL_AFINIDAD_TOPOLOGICA, PERFIL_ENERGIA,
    Scheduler, perfil_por_objetivo,
)
from slice_orchestrator.vm_placement import EstrategiaPlacement, asignar_hosts


def hosts(n=3, vcpu=8, ram=16384):
    return [Host(id=f"w{i}", nombre=f"w{i}", uri_libvirt=f"sim://w{i}", capacidad_vcpu=vcpu,
                 capacidad_ram_mb=ram, zona_disponibilidad="az") for i in range(1, n + 1)]


def lineal(n, vcpu=1, ram=512):
    nodos = [Nodo(id=f"n{i}", nombre=f"vm{i}", vcpu=vcpu, ram_mb=ram, disco_gb=1)
             for i in range(1, n + 1)]
    enlaces = [Enlace(id=f"e{i}", nodo_origen=f"n{i}", nodo_destino=f"n{i+1}") for i in range(1, n)]
    return Slice(id="s", nombre="s", owner="u", tipo_topologia=TipoTopologia.LINEAL,
                 nodos=nodos, enlaces=enlaces, estado=EstadoSlice.CREANDO, zona_disponibilidad="az")


def programar(objetivo, slc, hs):
    cfg = ConfigScheduler(monitorear=False, objetivo=objetivo, tiempo_limite_s=0)
    return Scheduler(config=cfg).programar(slc, hs).asignacion


class TestPerfil(unittest.TestCase):
    def test_por_defecto_balance(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORQ_SCHEDULER_OBJETIVO", None)
            nombre, pesadores = perfil_por_objetivo()
        self.assertEqual(nombre, "balance")
        self.assertEqual([p.nombre for p in pesadores], [p.nombre for p in PERFIL_AFINIDAD_TOPOLOGICA])

    def test_desde_entorno(self):
        with mock.patch.dict(os.environ, {"ORQ_SCHEDULER_OBJETIVO": "Energia"}):
            self.assertEqual(perfil_por_objetivo()[0], "energia")
            self.assertEqual(ConfigScheduler().objetivo, "energia")

    def test_objetivo_desconocido(self):
        with self.assertRaises(ValueError):
            perfil_por_objetivo("rapidez")

    def test_pesadores_explicitos_mandan(self):
        cfg = ConfigScheduler(pesadores=list(PERFIL_ENERGIA))
        self.assertEqual(cfg.objetivo, "personalizado")


class TestPesadorEnergia(unittest.TestCase):
    def test_prefiere_host_encendido(self):
        p = PesadorEnergia()
        on, off = hosts(2)
        on.vcpu_usados = 2
        nodo = Nodo(id="n", nombre="n", vcpu=1)
        self.assertGreater(p.puntuar(on, nodo, {}), p.puntuar(off, nodo, {}))

    def test_entre_encendidos_prefiere_el_mas_lleno(self):
        p = PesadorEnergia()
        a, b = hosts(2)
        a.vcpu_usados, b.vcpu_usados = 6, 2
        nodo = Nodo(id="n", nombre="n", vcpu=1)
        self.assertGreater(p.puntuar(a, nodo, {}), p.puntuar(b, nodo, {}))


class TestSchedulerEnergia(unittest.TestCase):
    def test_energia_consolida_y_balance_reparte(self):
        e = programar("energia", lineal(4), hosts())
        b = programar("balance", lineal(4), hosts())
        self.assertEqual(len(set(e.values())), 1)
        self.assertGreater(len(set(b.values())), 1)

    def test_usa_primero_los_hosts_ya_encendidos(self):
        hs = hosts()
        hs[2].vcpu_usados = 3
        hs[2].ram_usados_mb = 1024
        self.assertEqual(set(programar("energia", lineal(3), hs).values()), {"w3"})

    def test_respeta_la_capacidad(self):
        a = programar("energia", lineal(5), hosts(3, vcpu=2, ram=8192))
        usos = {h: list(a.values()).count(h) for h in set(a.values())}
        self.assertTrue(all(u <= 2 for u in usos.values()))
        self.assertEqual(len(usos), 3)

    def test_por_configuracion_en_la_fachada(self):
        with mock.patch.dict(os.environ, {"ORQ_SCHEDULER_OBJETIVO": "energia"}):
            a = asignar_hosts(lineal(4), hosts(), EstrategiaPlacement.OBJETIVO)
        self.assertEqual(len(set(a.values())), 1)


if __name__ == "__main__":
    unittest.main()
