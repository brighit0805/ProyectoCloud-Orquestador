"""
test_uplink_datos.py
--------------------
R5: uplink de datos separado. Las VLANs de las VMs salen por ens3 (directo a
ovs1) y ens4 (MPLink1, red de gestión) queda solo con la gestión, sin
etiqueta. Con y sin QinQ.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slice_orchestrator import control_broadcast as cb
from slice_orchestrator.models import EstadoSlice, TipoTopologia
from slice_orchestrator.network_manager import VLANAllocator, asignar_redes
from slice_orchestrator.topology_manager import construir_topologia

WORKERS = {h: {"ip": "x", "bridge": "br-int", "uplink": "ens4", "uplink_datos": "ens3"}
           for h in ("server1", "server2", "server3")}


def modulo(**extra):
    return types.SimpleNamespace(WORKERS=WORKERS, **extra)


def slice_ubicado(slice_id, n, hosts, allocator):
    slc = construir_topologia(slice_id, slice_id, "ana", TipoTopologia.LINEAL, n)
    asignar_redes(slc, allocator)
    for nodo, host in zip(slc.nodos, hosts):
        nodo.host_id = host
    slc.estado = EstadoSlice.ACTIVO
    return slc


def indice(cmds, texto):
    return next(i for i, c in enumerate(cmds) if texto in c)


class TestConfig(unittest.TestCase):
    def test_apagado_usa_ens4_como_siempre(self):
        cfg = cb.ConfigBroadcast.desde_modulo(modulo(UPLINK_DATOS_SEPARADO=False))
        self.assertEqual(cfg.uplink_por_host["server1"], "ens4")
        self.assertEqual(cfg.gestion_por_host, {})

    def test_encendido_datos_por_ens3_y_gestion_por_ens4(self):
        cfg = cb.ConfigBroadcast.desde_modulo(modulo(UPLINK_DATOS_SEPARADO=True))
        self.assertEqual(cfg.uplink_por_host, {h: "ens3" for h in WORKERS})
        self.assertEqual(cfg.gestion_por_host, {h: "ens4" for h in WORKERS})

    def test_host_sin_uplink_datos_sigue_igual(self):
        workers = {**WORKERS, "server3": {"ip": "x", "bridge": "br-int", "uplink": "ens4"}}
        cfg = cb.ConfigBroadcast.desde_modulo(types.SimpleNamespace(
            WORKERS=workers, UPLINK_DATOS_SEPARADO=True))
        self.assertEqual(cfg.uplink_por_host["server3"], "ens4")
        self.assertNotIn("server3", cfg.gestion_por_host)

    def test_config_cluster_real_viene_apagado(self):
        import config_cluster
        self.assertFalse(config_cluster.UPLINK_DATOS_SEPARADO)
        self.assertEqual({d.get("uplink_datos") for d in config_cluster.WORKERS.values()}, {"ens3"})
        self.assertEqual(cb.ConfigBroadcast.desde_modulo(config_cluster).gestion_por_host, {})


class TestSinQinQ(unittest.TestCase):
    def setUp(self):
        a = VLANAllocator()
        # vm1-vm2 dentro de server1 (local), vm2-vm3 cruza a server2.
        self.slc = slice_ubicado("s1", 3, ["server1", "server1", "server2"], a)
        self.plan = cb.calcular_plan([self.slc])
        self.cfg = cb.ConfigBroadcast.desde_modulo(modulo(UPLINK_DATOS_SEPARADO=True))
        self.cmds = cb.comandos_host("server1", self.plan, self.cfg)
        self.salen = ",".join(map(str, sorted(self.plan.vlans_que_salen("server1"))))

    def test_ens4_queda_solo_con_gestion(self):
        self.assertIn("sudo ovs-vsctl set port ens4 vlan_mode=native-untagged tag=0 trunks=4094", self.cmds)

    def test_ens3_en_br_int_en_trunk_a_secas_con_las_vlans_que_cruzan(self):
        self.assertIn(f"sudo ovs-vsctl --may-exist add-port br-int ens3 "
                      f"-- set port ens3 vlan_mode=trunk trunks={self.salen}", self.cmds)
        # trunk a secas: lo no etiquetado (gestión) no da la vuelta por ovs1.
        self.assertFalse(any("ens3" in c and "native" in c for c in self.cmds))

    def test_se_poda_ens4_antes_de_dar_vlans_a_ens3(self):
        self.assertLess(indice(self.cmds, "set port ens4"), indice(self.cmds, "add-port br-int ens3"))

    def test_sube_ens3_sin_cortar_la_cadena(self):
        self.assertIn("{ sudo -n ip link set dev ens3 up || true; }", self.cmds)

    def test_no_toca_qinq(self):
        self.assertFalse(any("br-qinq" in c or "dot1q-tunnel" in c or "qnat" in c for c in self.cmds))

    def test_storm_control_sigue(self):
        self.assertTrue(any("add-flow br-int" in c and "meter:" in c for c in self.cmds))

    def test_revertir(self):
        rev = cb.comandos_revertir_host("server1", self.cfg)
        self.assertEqual(rev, ["sudo ovs-vsctl --if-exists del-port ens3",
                               "sudo ovs-vsctl clear port ens4 vlan_mode",
                               "sudo ovs-vsctl clear port ens4 tag",
                               "sudo ovs-vsctl clear port ens4 trunks"])

    @unittest.skipUnless(shutil.which("bash"), "requiere bash")
    def test_el_comando_remoto_es_sh_valido(self):
        # Ver nota en test_qinq.py (27-nov): "sh -n -c" no frenaba la ejecución
        # en el headnode real; con bash sí se respeta el no-exec.
        r = subprocess.run([shutil.which("bash"), "-n", "-c", " && ".join(self.cmds)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestConQinQ(unittest.TestCase):
    def setUp(self):
        a = VLANAllocator()
        self.s1 = slice_ubicado("s1", 2, ["server1", "server2"], a)
        self.s2 = slice_ubicado("s2", 2, ["server1", "server3"], a)
        self.plan = cb.calcular_plan([self.s1, self.s2])
        self.cfg = cb.ConfigBroadcast.desde_modulo(modulo(
            UPLINK_DATOS_SEPARADO=True, QINQ={"activo": True}))
        self.cmds = cb.comandos_host("server1", self.plan, self.cfg)

    def test_ens3_en_br_qinq_con_las_s_vlan(self):
        s = ",".join(map(str, sorted([self.s1.vlan_servicio, self.s2.vlan_servicio])))
        self.assertIn(f"sudo ovs-vsctl --may-exist add-port br-qinq ens3 "
                      f"-- set port ens3 vlan_mode=trunk trunks={s}", self.cmds)

    def test_ens4_no_se_mueve_y_no_hace_falta_el_puente_nativo(self):
        self.assertFalse(any("del-port br-int ens4" in c for c in self.cmds))
        self.assertFalse(any("qnat" in c for c in self.cmds))
        self.assertIn("sudo ovs-vsctl set port ens4 vlan_mode=native-untagged tag=0 trunks=4094", self.cmds)

    def test_un_dot1q_tunnel_por_slice(self):
        for slc in (self.s1, self.s2):
            s = slc.vlan_servicio
            self.assertTrue(any(f"set port qe{s} vlan_mode=dot1q-tunnel tag={s}" in c for c in self.cmds))
        self.assertIn("sudo ovs-vsctl set Open_vSwitch . other_config:vlan-limit=2", self.cmds)

    def test_orden_bridge_antes_que_puerto(self):
        self.assertLess(indice(self.cmds, "add-br br-qinq"), indice(self.cmds, "add-port br-qinq ens3"))
        self.assertLess(indice(self.cmds, "set port ens4"), indice(self.cmds, "add-port br-qinq ens3"))

    def test_ovs1_poda_por_s_vlan_sin_dot1q_tunnel(self):
        cfg = cb.ConfigBroadcast.desde_modulo(modulo(
            UPLINK_DATOS_SEPARADO=True, QINQ={"activo": True},
            SWITCH_OVS={"destino_ssh": "ubuntu@192.168.201.5",
                        "puerto_por_host": {"server1": "ens4", "server2": "ens5", "server3": "ens6"}}))
        cmds = cb.comandos_switch(self.plan, cfg)
        self.assertIn(f"sudo ovs-vsctl set port ens5 vlan_mode=native-untagged tag=0 "
                      f"trunks={self.s1.vlan_servicio}", cmds)
        self.assertFalse(any("dot1q-tunnel" in c for c in cmds))

    def test_revertir_no_mueve_ens4(self):
        rev = cb.comandos_revertir_host("server1", self.cfg)
        self.assertFalse(any("br-qinq" in c and "ens4" in c for c in rev))
        self.assertIn("sudo ovs-vsctl --if-exists del-port ens3", rev)

    @unittest.skipUnless(shutil.which("bash"), "requiere bash")
    def test_el_comando_remoto_es_sh_valido(self):
        # Ver nota en test_qinq.py (27-nov): "sh -n -c" no frenaba la ejecución
        # en el headnode real; con bash sí se respeta el no-exec.
        r = subprocess.run([shutil.which("bash"), "-n", "-c", " && ".join(self.cmds)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
