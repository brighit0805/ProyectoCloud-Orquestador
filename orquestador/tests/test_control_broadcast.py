"""
test_control_broadcast.py
-------------------------
R5: limitar el broadcast a los puertos y servidores con VMs de la subred.

Cubre el plan (VLANs por servidor, cuáles cruzan el uplink), los comandos
OVS/OpenFlow que se generan, su aplicación por el driver Linux (simulado y
real con SSH falso), la integración con el servicio de slices y las redes
provider VLAN del driver OpenStack.
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slice_orchestrator import control_broadcast as cb
from slice_orchestrator.direccionamiento import interfaces_de
from slice_orchestrator.driver_linux import DriverLinux, DriverLinuxError, ModoDriver
from slice_orchestrator.driver_openstack import ConfigOpenStack, DriverOpenStack, SesionSimulada
from slice_orchestrator.models import EstadoSlice, TipoTopologia
from slice_orchestrator.network_manager import VLANAllocator, asignar_redes
from slice_orchestrator.servicio_slices import ServicioSlices
from slice_orchestrator.topology_manager import construir_topologia


def slice_ubicado(slice_id, tipo, n, hosts, allocator, estado=EstadoSlice.ACTIVO):
    """Slice con VLANs asignadas y nodos ubicados en ``hosts`` (uno por nodo, en orden)."""
    slc = construir_topologia(slice_id, slice_id, "ana", tipo, n)
    asignar_redes(slc, allocator)
    for nodo, host in zip(slc.nodos, hosts):
        nodo.host_id = host
    slc.estado = estado
    return slc


def vlan_entre(slc, a, b):
    """VLAN del enlace entre los nodos de índice a y b."""
    ida, idb = slc.nodos[a].id, slc.nodos[b].id
    for e in slc.enlaces:
        if {e.nodo_origen, e.nodo_destino} == {ida, idb}:
            return e.vlan_id
    raise KeyError((a, b))


CFG = cb.ConfigBroadcast(
    bridge_por_host={"server1": "br-provider", "server2": "br-provider", "server3": "br-provider"},
    uplink_por_host={"server1": "ens4", "server2": "ens4", "server3": "ens4"},
    switch=cb.ConfigSwitch("ubuntu@192.168.201.15",
                           {"server1": "eth1", "server2": "eth2", "server3": "eth3"}),
)


class TestPlan(unittest.TestCase):
    def setUp(self):
        # Lineal s1-s1-s2: el enlace vm1-vm2 queda dentro de server1 y vm2-vm3 cruza a server2.
        self.slc = slice_ubicado("L", TipoTopologia.LINEAL, 3, ["server1", "server1", "server2"],
                                 VLANAllocator())
        self.local = vlan_entre(self.slc, 0, 1)
        self.cruza = vlan_entre(self.slc, 1, 2)
        self.plan = cb.calcular_plan([self.slc])

    def test_vlans_por_servidor(self):
        self.assertEqual(self.plan.vlans_por_host["server1"], {self.local, self.cruza})
        self.assertEqual(self.plan.vlans_por_host["server2"], {self.cruza})
        self.assertNotIn("server3", self.plan.vlans_por_host)

    def test_solo_cruzan_el_uplink_las_vlans_compartidas(self):
        self.assertEqual(self.plan.vlans_que_salen("server1"), {self.cruza})
        self.assertEqual(self.plan.vlans_que_salen("server2"), {self.cruza})
        self.assertEqual(self.plan.vlans_que_salen("server3"), set())

    def test_un_puerto_por_interfaz_de_enlace(self):
        macs = {p.mac for ps in self.plan.puertos_por_host.values() for p in ps}
        esperadas = {i.mac for n in self.slc.nodos for i in interfaces_de(self.slc, n)}
        self.assertEqual(macs, esperadas)
        self.assertEqual(len(self.plan.puertos_por_host["server1"]), 3)   # vm1:1 + vm2:2
        self.assertEqual(len(self.plan.puertos_por_host["server2"]), 1)

    def test_ignora_slices_no_activos_y_nodos_sin_host(self):
        borrador = slice_ubicado("B", TipoTopologia.LINEAL, 2, ["server3", "server3"],
                                 VLANAllocator(inicio=3500, fin=3599), estado=EstadoSlice.BORRADOR)
        sin_host = slice_ubicado("H", TipoTopologia.LINEAL, 2, [None, None],
                                 VLANAllocator(inicio=3600, fin=3699))
        plan = cb.calcular_plan([self.slc, borrador, sin_host])
        self.assertEqual(set(plan.vlans_por_host), {"server1", "server2"})

    def test_bus_usa_una_sola_vlan(self):
        bus = slice_ubicado("BUS", TipoTopologia.BUS, 3, ["server1", "server2", "server3"],
                            VLANAllocator())
        plan = cb.calcular_plan([bus])
        vlans = set().union(*plan.vlans_por_host.values())
        self.assertEqual(len(vlans), 1)
        for h in ("server1", "server2", "server3"):
            self.assertEqual(plan.vlans_que_salen(h), vlans)

    def test_varios_slices_se_suman(self):
        otro = slice_ubicado("O", TipoTopologia.LINEAL, 2, ["server2", "server3"],
                             VLANAllocator(inicio=3200, fin=3299))
        plan = cb.calcular_plan([self.slc, otro])
        v = otro.enlaces[0].vlan_id
        self.assertEqual(plan.hosts_de_vlan(v), {"server2", "server3"})
        self.assertIn(v, plan.vlans_que_salen("server3"))
        self.assertNotIn(v, plan.vlans_que_salen("server1"))

    def test_resumen(self):
        filas = {f["host"]: f for f in self.plan.resumen()}
        self.assertEqual(filas["server1"]["vlans_solo_locales"], [self.local])
        self.assertEqual(filas["server1"]["vlans_uplink"], [self.cruza])
        self.assertEqual(filas["server1"]["puertos_con_storm_control"], 3)


class TestComandos(unittest.TestCase):
    def setUp(self):
        self.slc = slice_ubicado("L", TipoTopologia.LINEAL, 3, ["server1", "server1", "server2"],
                                 VLANAllocator())
        self.cruza = vlan_entre(self.slc, 1, 2)
        self.plan = cb.calcular_plan([self.slc])

    def test_uplink_solo_con_vlans_que_cruzan(self):
        cmds = cb.comandos_host("server1", self.plan, CFG)
        self.assertIn(f"sudo ovs-vsctl set port ens4 vlan_mode=native-untagged tag=0 trunks={self.cruza}", cmds)

    def test_uplink_sin_vlans_usa_vlan_reservada(self):
        """trunks vacío en OVS significa TODAS las VLANs: nunca se deja vacío."""
        cmds = cb.comandos_host("server3", self.plan, CFG)
        self.assertIn("sudo ovs-vsctl set port ens4 vlan_mode=native-untagged tag=0 trunks=4094", cmds)
        self.assertFalse(any("add-meter" in c for c in cmds))

    def test_storm_control_un_meter_y_una_regla_por_puerto(self):
        cmds = cb.comandos_host("server1", self.plan, CFG)
        meters = [c for c in cmds if "add-meter" in c]
        reglas = [c for c in cmds if "add-flow" in c and "priority=200" in c]
        self.assertEqual(len(meters), 3)
        self.assertEqual(len(reglas), 3)
        self.assertTrue(all("pktps" in m and "rate=200" in m and "burst_size=100" in m for m in meters))
        for p, regla in zip(self.plan.puertos_por_host["server1"], reglas):
            self.assertIn(f"dl_src={p.mac}", regla)
            self.assertIn(f"dl_dst={cb.BROADCAST_Y_MULTICAST}", regla)
            self.assertIn(",NORMAL", regla)

    def test_idempotente_borra_solo_lo_propio_antes_de_crear(self):
        cmds = cb.comandos_host("server1", self.plan, CFG)
        i_borrar = next(i for i, c in enumerate(cmds) if "del-flows" in c)
        i_crear = next(i for i, c in enumerate(cmds) if "add-flow" in c)
        self.assertLess(i_borrar, i_crear)
        self.assertIn("cookie=0xb0ca57/-1", cmds[i_borrar])
        self.assertEqual(cb.comandos_host("server1", self.plan, CFG), cmds)

    def test_bridge_habilita_openflow13(self):
        self.assertEqual(cb.comandos_host("server1", self.plan, CFG)[0],
                         "sudo ovs-vsctl set bridge br-provider protocols=OpenFlow10,OpenFlow13")

    def test_sin_uplink_no_toca_trunks(self):
        cfg = cb.ConfigBroadcast(bridge_por_host={"server1": "br-provider"})
        self.assertFalse(any("trunks" in c for c in cb.comandos_host("server1", self.plan, cfg)))

    def test_sin_storm_control(self):
        cfg = cb.ConfigBroadcast(bridge_por_host={"server1": "br-provider"}, storm_control=False)
        cmds = cb.comandos_host("server1", self.plan, cfg)
        self.assertFalse(any("ovs-ofctl" in c for c in cmds))

    def test_sin_bridge_no_genera_nada(self):
        self.assertEqual(cb.comandos_host("server1", self.plan, cb.ConfigBroadcast()), [])

    def test_switch_poda_cada_puerto(self):
        cmds = cb.comandos_switch(self.plan, CFG)
        self.assertEqual(cmds, [
            f"sudo ovs-vsctl set port eth1 vlan_mode=native-untagged tag=0 trunks={self.cruza}",
            f"sudo ovs-vsctl set port eth2 vlan_mode=native-untagged tag=0 trunks={self.cruza}",
            "sudo ovs-vsctl set port eth3 vlan_mode=native-untagged tag=0 trunks=4094",
        ])

    def test_sin_switch_no_toca_ovs1(self):
        self.assertEqual(cb.comandos_switch(self.plan, cb.ConfigBroadcast()), [])


class TestConfigDesdeModulo(unittest.TestCase):
    def test_lee_bridges_uplinks_y_switch(self):
        modulo = types.SimpleNamespace(
            WORKERS={"server1": {"ip": "x", "bridge": "br-int", "uplink": "ens4"},
                     "server2": {"ip": "y", "bridge": "br-int"}},
            SWITCH_OVS={"destino_ssh": "ubuntu@ovs1", "puerto_por_host": {"server1": "p1"}},
            CONTROL_BROADCAST={"limite_pps": 50, "storm_control": False})
        cfg = cb.ConfigBroadcast.desde_modulo(modulo)
        self.assertEqual(cfg.bridge_por_host, {"server1": "br-int", "server2": "br-int"})
        self.assertEqual(cfg.uplink_por_host, {"server1": "ens4"})
        self.assertEqual(cfg.switch.puerto_por_host, {"server1": "p1"})
        self.assertEqual((cfg.limite_pps, cfg.rafaga, cfg.storm_control), (50, 100, False))

    def test_campos_opcionales_ausentes(self):
        cfg = cb.ConfigBroadcast.desde_modulo(types.SimpleNamespace(WORKERS={}))
        self.assertIsNone(cfg.switch)
        self.assertEqual(cfg.uplink_por_host, {})

    def test_config_cluster_real_poda_uplinks_y_ovs1(self):
        """Uplinks de los servers confirmados (ens4, 24-nov) y puertos de ovs1
        hacia cada server (ens4/ens5/ens6, 26-nov)."""
        import config_cluster
        cfg = cb.ConfigBroadcast.desde_modulo(config_cluster)
        self.assertEqual(cfg.uplink_por_host,
                         {h: "ens4" for h in config_cluster.WORKERS})
        self.assertEqual(cfg.switch.puerto_por_host,
                         {"server1": "ens4", "server2": "ens5", "server3": "ens6"})
        self.assertEqual(set(cfg.bridge_por_host), set(config_cluster.WORKERS))


class TestDriverLinux(unittest.TestCase):
    def setUp(self):
        self.slc = slice_ubicado("L", TipoTopologia.LINEAL, 3, ["server1", "server1", "server2"],
                                 VLANAllocator())
        self.plan = cb.calcular_plan([self.slc])
        self.uris = {h: f"qemu+ssh://ubuntu@10.0.0.{i}/system"
                     for i, h in enumerate(["server1", "server2", "server3"], start=1)}

    def test_simulado_registra_sin_ejecutar(self):
        d = DriverLinux()
        with mock.patch.object(DriverLinux, "_ejecutar_ssh") as ssh:
            rep = d.aplicar_control_broadcast(self.plan, CFG, self.uris)
        ssh.assert_not_called()
        self.assertEqual(set(rep["hosts"]), {"server1", "server2", "server3"})
        self.assertIsNotNone(rep["switch"])
        self.assertEqual(rep["errores"], [])
        self.assertEqual(d.ultimo_control_broadcast, rep)
        self.assertIn(("control_broadcast", self.uris["server1"], "server1"), d.historial)

    def test_real_un_ssh_por_servidor_y_uno_al_switch(self):
        d = DriverLinux(modo=ModoDriver.REAL)
        with mock.patch.object(DriverLinux, "_ejecutar_ssh") as ssh:
            d.aplicar_control_broadcast(self.plan, CFG, self.uris)
        destinos = [c.args[0] for c in ssh.call_args_list]
        self.assertEqual(destinos, ["ubuntu@10.0.0.1", "ubuntu@10.0.0.2", "ubuntu@10.0.0.3",
                                    "ubuntu@192.168.201.15"])
        primero = ssh.call_args_list[0].args[1]
        # Va envuelto en el vigía de auto-reversión: primero el vigía en
        # segundo plano, luego los comandos reales y al final el marcador.
        self.assertTrue(primero.startswith("(sleep 30; "))
        self.assertIn(" && ".join(cb.comandos_host("server1", self.plan, CFG)) + " && touch ", primero)
        for c in cb.comandos_revertir_host("server1", CFG):
            self.assertIn(c, primero)

    def test_real_un_error_no_detiene_a_los_demas(self):
        d = DriverLinux(modo=ModoDriver.REAL)

        def ssh(destino, comando):
            if destino == "ubuntu@10.0.0.2":
                raise DriverLinuxError("ovs-vsctl: no bridge named br-provider")
        with mock.patch.object(DriverLinux, "_ejecutar_ssh", side_effect=ssh) as m:
            rep = d.aplicar_control_broadcast(self.plan, CFG, self.uris)
        self.assertEqual(m.call_count, 4)
        self.assertEqual(len(rep["errores"]), 1)
        self.assertTrue(rep["errores"][0].startswith("server2:"))

    def test_no_cuenta_como_operacion_para_las_fallas_simuladas(self):
        d = DriverLinux(fallar_en_operacion=1)
        d.aplicar_control_broadcast(self.plan, CFG, self.uris)     # no debe lanzar
        with self.assertRaises(DriverLinuxError):
            d.crear_red("sim://worker1", "red", 3000)


class TestServicio(unittest.TestCase):
    def spec(self, nombre):
        return {"nombre": nombre, "topologia": {"base": {"tipo": "anillo", "nodos": 3}}}

    def test_se_aplica_al_desplegar_editar_y_eliminar(self):
        driver = DriverLinux()
        svc = ServicioSlices(driver=driver)
        with mock.patch.object(DriverLinux, "aplicar_control_broadcast",
                               autospec=True, return_value={"errores": []}) as aplicar:
            d = svc.crear_slice("ana", self.spec("a"), desplegar=True, estrategia="round_robin")
            self.assertEqual(aplicar.call_count, 1)
            plan = aplicar.call_args.args[1]
            self.assertEqual(set(plan.vlans_por_host), {"worker1", "worker2", "worker3"})
            svc.crear_slice("ana", self.spec("borrador"))           # sin desplegar: no aplica
            self.assertEqual(aplicar.call_count, 1)
            svc.eliminar_slice("ana", d["id"])
            self.assertEqual(aplicar.call_count, 2)
            self.assertEqual(aplicar.call_args.args[1].vlans_por_host, {})

    def test_un_error_del_driver_no_tumba_el_despliegue(self):
        svc = ServicioSlices(driver=DriverLinux())
        with mock.patch.object(DriverLinux, "aplicar_control_broadcast", side_effect=RuntimeError("x")):
            d = svc.crear_slice("ana", self.spec("a"), desplegar=True, estrategia="round_robin")
        self.assertEqual(d["estado"], "activo")

    def test_estado_broadcast(self):
        svc = ServicioSlices(driver=DriverLinux())
        self.assertEqual(svc.estado_broadcast(), [])
        svc.crear_slice("ana", self.spec("a"), desplegar=True, estrategia="round_robin")
        filas = svc.estado_broadcast()
        self.assertEqual([f["host"] for f in filas], ["worker1", "worker2", "worker3"])
        # Anillo de 3 en 3 hosts: cada VLAN une dos servidores, todas cruzan el uplink.
        self.assertTrue(all(f["vlans_solo_locales"] == [] for f in filas))
        self.assertTrue(all(f["puertos_con_storm_control"] == 2 for f in filas))

    def test_driver_sin_soporte_se_ignora(self):
        svc = ServicioSlices(driver=DriverLinux())
        svc._driver = object()
        self.assertIsNone(svc._actualizar_control_broadcast())


class TestCLI(unittest.TestCase):
    def test_comando_red_broadcast(self):
        import io
        from contextlib import redirect_stdout
        from slice_orchestrator.cli import main
        svc = ServicioSlices(driver=DriverLinux())
        svc.crear_slice("ana", {"nombre": "a", "topologia": {"base": {"tipo": "lineal", "nodos": 3}}},
                        desplegar=True, estrategia="round_robin")
        salida = io.StringIO()
        with redirect_stdout(salida):
            self.assertEqual(main(["red", "broadcast"], servicio=svc), 0)
        texto = salida.getvalue()
        self.assertIn("cruzan el uplink", texto)
        self.assertIn("worker1", texto)


class TestOpenStackProvider(unittest.TestCase):
    def driver(self, **cfg):
        c = ConfigOpenStack(dominio_id="dom", proyecto_admin_id="adm", usuario_admin_id="u-admin",
                            password_admin="x", rol_admin_id="rol-admin", **cfg)
        d = DriverOpenStack(config=c, imagen_id_defecto="img-cirros", flavor_id_defecto="flv-512",
                            espera_reintento_s=0.0)
        d.sesion = SesionSimulada(c)
        d.proyecto_id = d._api("admin", "POST", c.keystone + "/projects", esperado=(201,),
                               cuerpo={"project": {"name": "p", "domain_id": "dom"}}).json()["project"]["id"]
        return d

    def red(self, d, nombre):
        return next(r for r in d.sesion.redes.values() if r["name"] == nombre)

    def test_con_physnet_crea_red_provider_vlan(self):
        d = self.driver(red_fisica_provider="physnet1")
        d.crear_red("openstack://nova:worker1", "enlace-a", 3007)
        r = self.red(d, "enlace-a")
        self.assertEqual(r["provider:network_type"], "vlan")
        self.assertEqual(r["provider:physical_network"], "physnet1")
        self.assertEqual(r["provider:segmentation_id"], 3007)

    def test_sin_physnet_queda_como_antes(self):
        d = self.driver()
        d.crear_red("openstack://nova:worker1", "enlace-a", 3007)
        self.assertFalse(any(k.startswith("provider:") for k in self.red(d, "enlace-a")))

    def test_desde_entorno(self):
        self.assertEqual(ConfigOpenStack.desde_entorno({"PROVIDER_PHYSNET": "physnet2"})
                         .red_fisica_provider, "physnet2")
        self.assertEqual(ConfigOpenStack.desde_entorno({}).red_fisica_provider, "")


if __name__ == "__main__":
    unittest.main()
