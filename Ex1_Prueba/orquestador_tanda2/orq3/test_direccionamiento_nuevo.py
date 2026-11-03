"""Pruebas de direccionamiento y semilla cloud-init (modo simulado)."""
import unittest

from slice_orchestrator import direccionamiento as d
from slice_orchestrator.driver_linux import DriverLinux
from slice_orchestrator.models import Host
from slice_orchestrator.network_manager import VLANAllocator, asignar_redes
from slice_orchestrator.orchestrator import desplegar_slice_existente, eliminar_slice
from slice_orchestrator.topology_manager import construir_ex1_topology
from slice_orchestrator.vm_placement import EstrategiaPlacement

ACCESO = {"ips": {"vm1": "10.0.10.31", "vm3": "10.0.10.33"},
          "prefijo": 24, "gateway": "10.0.10.100", "vlan": None}


def hosts():
    return [Host(f"worker{i}", f"worker{i}", f"qemu+ssh://ubuntu@10.0.10.{i}/system", 16, 32768, "az-lab")
            for i in (1, 2, 3)]


def slice_ex1():
    return construir_ex1_topology("slice-ex1", "demo-ex1", "g2@pucp.edu.pe", "az-lab")


class TestDireccionamiento(unittest.TestCase):
    def test_ips_coinciden_con_las_validadas_a_mano(self):
        s = slice_ex1()
        asignar_redes(s, VLANAllocator(3000, 3099))
        esperado = {
            "demo-ex1-vm1": ["10.30.0.1/30", "10.30.3.2/30"],
            "demo-ex1-vm2": ["10.30.0.2/30", "10.30.1.1/30"],
            "demo-ex1-vm3": ["10.30.1.2/30", "10.30.2.1/30"],
            "demo-ex1-vm4": ["10.30.2.2/30", "10.30.3.1/30", "10.30.4.1/30"],
            "demo-ex1-vm5": ["10.30.4.2/30", "10.30.5.1/30"],
            "demo-ex1-vm6": ["10.30.5.2/30"],
        }
        for n in s.nodos:
            self.assertEqual([i.cidr for i in d.interfaces_de(s, n)], esperado[n.nombre])

    def test_macs_deterministas_y_distintas(self):
        s = slice_ex1()
        asignar_redes(s, VLANAllocator(3000, 3099))
        macs1 = [i.mac for n in s.nodos for i in d.interfaces_de(s, n)]
        macs2 = [i.mac for n in s.nodos for i in d.interfaces_de(s, n)]
        self.assertEqual(macs1, macs2)
        self.assertEqual(len(macs1), len(set(macs1)))

    def test_semilla_ubuntu_y_cirros(self):
        s = slice_ex1()
        asignar_redes(s, VLANAllocator(3000, 3099))
        vm1, vm2 = s.nodos[0], s.nodos[1]
        sem1 = d.archivos_semilla(vm1, d.interfaces_de(s, vm1))
        sem2 = d.archivos_semilla(vm2, d.interfaces_de(s, vm2))
        self.assertIn("network-config", sem1)          # Ubuntu: red persistente
        self.assertIn("10.30.0.1/30", sem1["network-config"])
        self.assertNotIn("network-config", sem2)       # Cirros: script
        self.assertTrue(sem2["user-data"].startswith("#!/bin/sh"))
        self.assertIn("10.30.0.2/30", sem2["user-data"])

    def test_meta_data_de_cirros_es_json_valido(self):
        """El NoCloud de Cirros parsea meta-data con json2fstree (JSON). Con YAML
        descarta el datasource y no ejecuta el user-data (fallo visto en el clúster)."""
        import json
        s = slice_ex1()
        asignar_redes(s, VLANAllocator(3000, 3099))
        vm2 = s.nodos[1]
        meta = json.loads(d.archivos_semilla(vm2, d.interfaces_de(s, vm2))["meta-data"])
        self.assertEqual(meta["instance-id"], vm2.nombre)
        self.assertEqual(meta["local-hostname"], vm2.nombre)

    def test_meta_data_de_ubuntu_sigue_en_yaml(self):
        s = slice_ex1()
        asignar_redes(s, VLANAllocator(3000, 3099))
        vm1 = s.nodos[0]
        meta = d.archivos_semilla(vm1, d.interfaces_de(s, vm1))["meta-data"]
        self.assertTrue(meta.startswith("instance-id: "))


class TestDespliegueConSemilla(unittest.TestCase):
    def test_solo_vm1_y_vm3_llevan_interfaz_de_acceso(self):
        drv = DriverLinux()
        s = desplegar_slice_existente(slice_ex1(), hosts(), drv, VLANAllocator(3000, 3099),
                                      EstrategiaPlacement.ROUND_ROBIN, config_acceso=ACCESO)
        con_acceso = {}
        for (uri, vm), sem in drv.semillas_simuladas.items():
            con_acceso[vm] = "10.0.10." in (sem.get("network-config", "") + sem["user-data"])
        self.assertEqual({v for v, ok in con_acceso.items() if ok},
                         {"demo-ex1-vm1", "demo-ex1-vm3"})
        self.assertEqual(len(drv.vms_simuladas), 6)
        self.assertEqual(s.estado.value, "activo")

    def test_eliminar_borra_vms_y_semillas(self):
        drv, hs, alloc = DriverLinux(), hosts(), VLANAllocator(3000, 3099)
        s = desplegar_slice_existente(slice_ex1(), hs, drv, alloc,
                                      EstrategiaPlacement.ROUND_ROBIN, config_acceso=ACCESO)
        eliminar_slice(s, hs, drv, alloc)
        self.assertEqual(len(drv.vms_simuladas), 0)
        self.assertEqual(len(drv.semillas_simuladas), 0)

    def test_sin_config_acceso_no_hay_interfaz_extra(self):
        drv = DriverLinux()
        desplegar_slice_existente(slice_ex1(), hosts(), drv, VLANAllocator(3000, 3099),
                                  EstrategiaPlacement.ROUND_ROBIN)
        for sem in drv.semillas_simuladas.values():
            self.assertNotIn("10.0.10.", sem["user-data"] + sem.get("network-config", ""))


if __name__ == "__main__":
    unittest.main()


class TestXmlDominio(unittest.TestCase):
    def test_xml_valido_con_macs_semilla_y_acceso(self):
        import xml.etree.ElementTree as ET
        xml = DriverLinux()._xml_vm(
            "demo-ex1-vm1", 1, 512, "/var/lib/libvirt/images/demo-ex1-vm1.qcow2",
            [("r0", 3000), ("r3", 3003)], bridge="br-int",
            macs=["52:54:00:00:00:01", "52:54:00:00:00:02"],
            semilla="/var/lib/libvirt/images/seed-demo-ex1-vm1.iso",
            con_acceso=True, mac_acceso="52:54:00:00:00:03")
        raiz = ET.fromstring(xml)
        itfs = raiz.findall("./devices/interface")
        self.assertEqual(len(itfs), 3)                              # 2 enlaces + acceso
        self.assertEqual(itfs[0].find("mac").get("address"), "52:54:00:00:00:01")
        self.assertEqual(itfs[0].find("vlan/tag").get("id"), "3000")
        self.assertIsNone(itfs[2].find("vlan"))                     # acceso sin etiqueta
        self.assertIsNotNone(raiz.find("./devices/disk[@device='cdrom']"))


class TestAccesoNatLibvirt(unittest.TestCase):
    def test_nic_nat_libvirt_con_dhcp(self):
        from slice_orchestrator.direccionamiento import InterfazAcceso, network_config_ubuntu
        from slice_orchestrator.driver_linux import DriverLinux
        nic = InterfazAcceso(mac="52:54:00:aa:bb:cc", red_libvirt="default")
        cfg = network_config_ubuntu([], nic)
        self.assertIn("dhcp4: true", cfg)
        self.assertNotIn("gateway4", cfg)
        xml = DriverLinux()._xml_vm("v", 1, 512, "/x.qcow2", [], con_acceso=True,
                                    mac_acceso=nic.mac, red_libvirt="default")
        self.assertIn("<interface type='network'>", xml)
        self.assertIn("<source network='default'/>", xml)
