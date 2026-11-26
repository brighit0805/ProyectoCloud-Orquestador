"""
test_qinq.py
------------
Aislamiento entre slices con QinQ (802.1ad): una S-VLAN (etiqueta externa)
por slice y una C-VLAN (etiqueta interna) por enlace.

Cubre la reserva y persistencia de la S-VLAN, el plan por slice, los
comandos OVS que arman br-qinq y los puertos dot1q-tunnel, la poda por
S-VLAN en uplinks y ovs1, el MTU de las VMs y la migración de slices
desplegados antes de QinQ.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slice_orchestrator import control_broadcast as cb
from slice_orchestrator import serializacion
from slice_orchestrator.driver_linux import DriverLinux
from slice_orchestrator.models import EstadoSlice, TipoTopologia
from slice_orchestrator.network_manager import VLANAllocator, asignar_redes
from slice_orchestrator.servicio_slices import ServicioSlices
from slice_orchestrator.topology_manager import construir_topologia


def slice_ubicado(slice_id, tipo, n, hosts, allocator):
    slc = construir_topologia(slice_id, slice_id, "ana", tipo, n)
    asignar_redes(slc, allocator)
    for nodo, host in zip(slc.nodos, hosts):
        nodo.host_id = host
    slc.estado = EstadoSlice.ACTIVO
    return slc


def cfg_qinq(**kw):
    return cb.ConfigBroadcast(
        bridge_por_host={h: "br-int" for h in ("server1", "server2", "server3")},
        uplink_por_host={h: "ens4" for h in ("server1", "server2", "server3")},
        switch=cb.ConfigSwitch("ubuntu@192.168.201.5",
                               {"server1": "eth1", "server2": "eth2", "server3": "eth3"}),
        qinq=cb.ConfigQinQ(**kw))


class TestReservaSVLAN(unittest.TestCase):
    def test_una_por_slice_distinta_e_idempotente(self):
        a = VLANAllocator()
        s1 = a.reservar_servicio("s1")
        s2 = a.reservar_servicio("s2")
        self.assertNotEqual(s1, s2)
        self.assertEqual(a.reservar_servicio("s1"), s1)
        self.assertTrue(2000 <= s1 <= 2999 and 2000 <= s2 <= 2999)

    def test_liberar_la_devuelve(self):
        a = VLANAllocator()
        s1 = a.reservar_servicio("s1")
        a.liberar("s1")
        self.assertEqual(a.reservar_servicio("s3"), s1)

    def test_rango_agotado(self):
        a = VLANAllocator(inicio_servicio=2000, fin_servicio=2001)
        a.reservar_servicio("a")
        a.reservar_servicio("b")
        with self.assertRaises(RuntimeError):
            a.reservar_servicio("c")

    def test_restaurar_detecta_choque(self):
        a = VLANAllocator()
        a.restaurar("s1", [3000], 2005)
        with self.assertRaises(RuntimeError):
            a.restaurar("s2", [3001], 2005)
        self.assertNotEqual(a.reservar_servicio("s3"), 2005)

    def test_asignar_redes_pone_la_svlan(self):
        a = VLANAllocator()
        lineal = construir_topologia("l", "l", "ana", TipoTopologia.LINEAL, 3)
        bus = construir_topologia("b", "b", "ana", TipoTopologia.BUS, 3)
        asignar_redes(lineal, a)
        asignar_redes(bus, a)
        self.assertIsNotNone(lineal.vlan_servicio)
        self.assertIsNotNone(bus.vlan_servicio)
        self.assertNotEqual(lineal.vlan_servicio, bus.vlan_servicio)
        # La S-VLAN es de otro rango que las VLAN de enlace.
        self.assertTrue(all(e.vlan_id >= 3000 for e in lineal.enlaces))

    def test_serializacion(self):
        a = VLANAllocator()
        slc = slice_ubicado("s1", TipoTopologia.LINEAL, 2, ["server1", "server2"], a)
        d = serializacion.slice_a_dict(slc)
        self.assertEqual(serializacion.slice_desde_dict(d).vlan_servicio, slc.vlan_servicio)
        d.pop("vlan_servicio")                       # estado guardado antes de QinQ
        self.assertIsNone(serializacion.slice_desde_dict(d).vlan_servicio)


class TestPlanQinQ(unittest.TestCase):
    def setUp(self):
        a = VLANAllocator()
        # s1 cruza server1-server2; s2 vive entero en server3; s3 cruza server2-server3.
        self.s1 = slice_ubicado("s1", TipoTopologia.LINEAL, 3, ["server1", "server1", "server2"], a)
        self.s2 = slice_ubicado("s2", TipoTopologia.LINEAL, 2, ["server3", "server3"], a)
        self.s3 = slice_ubicado("s3", TipoTopologia.LINEAL, 2, ["server2", "server3"], a)
        self.plan = cb.calcular_plan([self.s1, self.s2, self.s3])

    def test_slices_que_salen(self):
        self.assertEqual(set(self.plan.slices_que_salen("server1")), {"s1"})
        self.assertEqual(set(self.plan.slices_que_salen("server2")), {"s1", "s3"})
        self.assertEqual(set(self.plan.slices_que_salen("server3")), {"s3"})   # s2 es local

    def test_solo_las_cvlan_que_cruzan(self):
        # En server1 el enlace vm1-vm2 de s1 es local: solo sale el de vm2-vm3.
        cruza = {e.vlan_id for e in self.s1.enlaces
                 if {self.s1.nodo(e.nodo_origen).host_id,
                     self.s1.nodo(e.nodo_destino).host_id} == {"server1", "server2"}}
        self.assertEqual(self.plan.slices_que_salen("server1")["s1"], cruza)

    def test_svlans_que_salen(self):
        self.assertEqual(self.plan.svlans_que_salen("server2"),
                         {self.s1.vlan_servicio, self.s3.vlan_servicio})
        self.assertNotIn(self.s2.vlan_servicio, self.plan.svlans_que_salen("server3"))

    def test_resumen(self):
        fila = {f["host"]: f for f in self.plan.resumen()}["server1"]
        self.assertEqual(fila["svlans_uplink"], [self.s1.vlan_servicio])


class TestComandosQinQ(unittest.TestCase):
    def setUp(self):
        a = VLANAllocator()
        self.s1 = slice_ubicado("s1", TipoTopologia.LINEAL, 2, ["server1", "server2"], a)
        self.s2 = slice_ubicado("s2", TipoTopologia.LINEAL, 2, ["server1", "server2"], a)
        self.plan = cb.calcular_plan([self.s1, self.s2])
        self.cfg = cfg_qinq()
        self.cmds = cb.comandos_host("server1", self.plan, self.cfg)
        self.texto = "\n".join(self.cmds)

    def test_prepara_bridge_y_mueve_el_uplink(self):
        self.assertIn("sudo ovs-vsctl set Open_vSwitch . other_config:vlan-limit=2", self.cmds)
        self.assertIn("sudo ovs-vsctl --may-exist add-br br-qinq", self.cmds)
        self.assertIn("sudo ovs-vsctl --if-exists del-port br-int ens4 "
                      "-- --may-exist add-port br-qinq ens4", self.cmds)

    def test_uplink_lleva_solo_svlans(self):
        s = sorted([self.s1.vlan_servicio, self.s2.vlan_servicio])
        self.assertIn(f"sudo ovs-vsctl set port ens4 vlan_mode=trunk trunks={s[0]},{s[1]}",
                      self.cmds)
        c_vlan = self.s1.enlaces[0].vlan_id
        self.assertNotIn(f"trunks={c_vlan}", " ".join(c for c in self.cmds if " ens4 " in c))

    def test_puerto_dot1q_tunnel_por_slice(self):
        s, c = self.s1.vlan_servicio, self.s1.enlaces[0].vlan_id
        self.assertIn(f"sudo ovs-vsctl --may-exist add-port br-int qi{s} "
                      f"-- set interface qi{s} type=patch options:peer=qe{s} "
                      f"-- set port qi{s} vlan_mode=trunk trunks={c}", self.cmds)
        self.assertIn(f"sudo ovs-vsctl --may-exist add-port br-qinq qe{s} "
                      f"-- set interface qe{s} type=patch options:peer=qi{s} "
                      f"-- set port qe{s} vlan_mode=dot1q-tunnel tag={s} cvlans={c} "
                      f"other_config:qinq-ethtype=802.1ad", self.cmds)

    def test_ethertype_configurable(self):
        cmds = cb.comandos_host("server1", self.plan, cfg_qinq(ethertype="802.1q"))
        self.assertIn("other_config:qinq-ethtype=802.1q", "\n".join(cmds))
        with self.assertRaises(ValueError):
            cb.ConfigQinQ(ethertype="0x9100")

    def test_limpia_patches_de_slices_eliminados(self):
        s1, s2 = self.s1.vlan_servicio, self.s2.vlan_servicio
        limpiar = [c for c in self.cmds if c.startswith("for p in")]
        self.assertEqual(len(limpiar), 2)
        self.assertIn(f'" qi{s1} qi{s2} "', limpiar[0])
        self.assertIn(f'" qe{s1} qe{s2} "', limpiar[1])

    def test_sin_slices_no_pasa_nada_por_el_uplink(self):
        cmds = cb.comandos_host("server1", cb.calcular_plan([]), self.cfg)
        self.assertIn("sudo ovs-vsctl set port ens4 vlan_mode=trunk trunks=4094", cmds)
        self.assertFalse(any("add-port br-int qi" in c for c in cmds))
        limpiar = [c for c in cmds if c.startswith("for p in")]
        self.assertTrue(limpiar and all('case "  " in' in c for c in limpiar))   # borra todos

    def test_slice_local_no_crea_patch(self):
        a = VLANAllocator()
        local = slice_ubicado("loc", TipoTopologia.LINEAL, 2, ["server3", "server3"], a)
        cmds = cb.comandos_host("server3", cb.calcular_plan([local]), self.cfg)
        self.assertFalse(any(f"qi{local.vlan_servicio}" in c and "add-port" in c for c in cmds))

    def test_storm_control_sigue_en_br_int(self):
        self.assertTrue(any("add-flow br-int" in c and "meter:" in c for c in self.cmds))

    def test_ovs1_poda_por_svlan(self):
        s = sorted([self.s1.vlan_servicio, self.s2.vlan_servicio])
        cmds = cb.comandos_switch(self.plan, self.cfg)
        self.assertIn(f"sudo ovs-vsctl set port eth1 vlan_mode=trunk trunks={s[0]},{s[1]}", cmds)
        self.assertIn("sudo ovs-vsctl set port eth3 vlan_mode=trunk trunks=4094", cmds)

    def test_sin_qinq_queda_como_antes(self):
        cfg = cb.ConfigBroadcast(bridge_por_host={"server1": "br-int"},
                                 uplink_por_host={"server1": "ens4"})
        cmds = cb.comandos_host("server1", self.plan, cfg)
        self.assertFalse(any("br-qinq" in c or "dot1q-tunnel" in c for c in cmds))
        self.assertTrue(any(c.startswith("sudo ovs-vsctl set port ens4 vlan_mode=trunk") for c in cmds))

    @unittest.skipUnless(shutil.which("sh"), "requiere sh")
    def test_el_comando_remoto_es_sh_valido(self):
        # El driver une los comandos con " && " y los corre por SSH en el server.
        r = subprocess.run([shutil.which("sh"), "-n", "-c", " && ".join(self.cmds)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestConfigQinQ(unittest.TestCase):
    def modulo(self, **extra):
        return types.SimpleNamespace(
            WORKERS={"server1": {"ip": "x", "bridge": "br-int", "uplink": "ens4"}}, **extra)

    def test_activo(self):
        cfg = cb.ConfigBroadcast.desde_modulo(self.modulo(
            QINQ={"activo": True, "bridge": "br-q", "ethertype": "802.1q", "mtu_vm": 1492}))
        self.assertEqual((cfg.qinq.bridge, cfg.qinq.ethertype, cfg.qinq.mtu_vm),
                         ("br-q", "802.1q", 1492))

    def test_inactivo_o_ausente(self):
        self.assertIsNone(cb.ConfigBroadcast.desde_modulo(self.modulo()).qinq)
        self.assertIsNone(cb.ConfigBroadcast.desde_modulo(
            self.modulo(QINQ={"activo": False})).qinq)

    def test_config_cluster_real(self):
        import config_cluster
        q = cb.ConfigBroadcast.desde_modulo(config_cluster).qinq
        self.assertIsNotNone(q)
        self.assertEqual(q.ethertype, "802.1ad")


class TestMTU(unittest.TestCase):
    def test_interfaces_de_enlace_con_mtu(self):
        d = DriverLinux(mtu_enlaces=1496)
        xml = d._xml_vm("vm1", 1, 512, "/v.qcow2", [("r1", 3000), ("r2", 3001)], bridge="br-int",
                        con_acceso=True, red_libvirt="default")
        self.assertEqual(xml.count("<mtu size='1496'/>"), 2)          # no en la NIC de acceso

    def test_sin_qinq_sin_mtu(self):
        xml = DriverLinux()._xml_vm("vm1", 1, 512, "/v.qcow2", [("r1", 3000)], bridge="br-int")
        self.assertNotIn("<mtu", xml)


class TestServicioQinQ(unittest.TestCase):
    def spec(self, nombre):
        return {"nombre": nombre, "topologia": {"base": {"tipo": "anillo", "nodos": 3}}}

    def test_svlan_distinta_por_slice_y_visible(self):
        svc = ServicioSlices(driver=DriverLinux())
        a = svc.crear_slice("ana", self.spec("a"), desplegar=True, estrategia="round_robin")
        b = svc.crear_slice("ana", self.spec("b"), desplegar=True, estrategia="round_robin")
        self.assertIsNotNone(a["vlan_servicio"])
        self.assertNotEqual(a["vlan_servicio"], b["vlan_servicio"])
        filas = svc.estado_broadcast()
        self.assertTrue(all(sorted(f["svlans_uplink"]) == sorted([a["vlan_servicio"],
                                                                   b["vlan_servicio"]])
                            for f in filas))

    def test_se_libera_al_eliminar(self):
        svc = ServicioSlices(driver=DriverLinux())
        a = svc.crear_slice("ana", self.spec("a"), desplegar=True, estrategia="round_robin")
        svc.eliminar_slice("ana", a["id"])
        b = svc.crear_slice("ana", self.spec("b"), desplegar=True, estrategia="round_robin")
        self.assertEqual(b["vlan_servicio"], a["vlan_servicio"])

    def test_slice_anterior_a_qinq_recibe_svlan(self):
        svc = ServicioSlices(driver=DriverLinux())
        a = svc.crear_slice("ana", self.spec("a"), desplegar=True, estrategia="round_robin")
        b = svc.crear_slice("ana", self.spec("b"), desplegar=True, estrategia="round_robin")
        svc._slices[a["id"]].vlan_servicio = None               # como si fuera de antes
        with mock.patch.object(DriverLinux, "aplicar_control_broadcast",
                               autospec=True, return_value={"errores": []}) as aplicar:
            svc._actualizar_control_broadcast()
        nueva = svc._slices[a["id"]].vlan_servicio
        self.assertIsNotNone(nueva)
        self.assertNotEqual(nueva, b["vlan_servicio"])
        self.assertEqual(aplicar.call_args.args[1].servicio_por_slice[a["id"]], nueva)


if __name__ == "__main__":
    unittest.main()
