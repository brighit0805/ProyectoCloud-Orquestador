"""Reintentos del despliegue, copia de imágenes y borrado inteligente (modo simulado)."""
import unittest
from unittest import mock

from slice_orchestrator.driver_linux import DriverLinux
from slice_orchestrator.models import EstadoSlice, Host
from slice_orchestrator.network_manager import VLANAllocator
from slice_orchestrator.orchestrator import (DespliegueFallidoError, desplegar_slice_existente,
                                             eliminar_slice)
from slice_orchestrator.topology_manager import construir_ex1_topology
from slice_orchestrator.vm_placement import EstrategiaPlacement

RR = EstrategiaPlacement.ROUND_ROBIN


def hosts():
    return [Host(f"worker{i}", f"worker{i}", f"qemu+ssh://ubuntu@10.0.10.{i}/system", 16, 32768, "az-lab")
            for i in (1, 2, 3)]


def ex1():
    return construir_ex1_topology("slice-ex1", "demo-ex1", "g2@pucp.edu.pe", "az-lab")


@mock.patch("slice_orchestrator.orchestrator.time.sleep", lambda s: None)
class TestReintentos(unittest.TestCase):
    def test_un_fallo_transitorio_se_recupera_con_reintentos(self):
        drv = DriverLinux(fallar_en_operacion=5)          # falla UNA operación
        s = desplegar_slice_existente(ex1(), hosts(), drv, VLANAllocator(3000, 3099), RR, reintentos=3)
        self.assertEqual(s.estado, EstadoSlice.ACTIVO)
        self.assertEqual(len(drv.vms_simuladas), 6)

    def test_sin_reintentos_el_mismo_fallo_provoca_rollback(self):
        drv = DriverLinux(fallar_en_operacion=5)
        with self.assertRaises(DespliegueFallidoError):
            desplegar_slice_existente(ex1(), hosts(), drv, VLANAllocator(3000, 3099), RR, reintentos=1)
        self.assertEqual(len(drv.vms_simuladas), 0)

    def test_si_se_agotan_los_reintentos_hay_rollback_sin_huerfanos(self):
        hs, alloc = hosts(), VLANAllocator(3000, 3099)
        drv = DriverLinux(fallar_en_operacion=9, fallas_consecutivas=3)   # falla las 3 veces
        with self.assertRaises(DespliegueFallidoError):
            desplegar_slice_existente(ex1(), hs, drv, alloc, RR, reintentos=3)
        self.assertEqual(len(drv.vms_simuladas), 0)
        self.assertEqual(len(drv.redes_simuladas), 0)
        self.assertEqual(len(drv.imagenes_simuladas), 0)      # tampoco quedan imágenes copiadas
        self.assertTrue(all(h.vcpu_usados == 0 for h in hs))


class TestImagenes(unittest.TestCase):
    def test_se_copian_solo_las_imagenes_necesarias_a_cada_worker(self):
        drv = DriverLinux()
        desplegar_slice_existente(ex1(), hosts(), drv, VLANAllocator(3000, 3099), RR)
        copias = {(h.split("@")[1].split("/")[0], i) for h, i in drv.imagenes_simuladas}
        # worker1: VM1,VM4 (ubuntu) · worker2: VM2 (cirros), VM5 (ubuntu) · worker3: VM3 (ubuntu), VM6 (cirros)
        self.assertEqual(copias, {
            ("10.0.10.1", "ubuntu-22.04.qcow2"),
            ("10.0.10.2", "cirros-0.6.2.qcow2"), ("10.0.10.2", "ubuntu-22.04.qcow2"),
            ("10.0.10.3", "ubuntu-22.04.qcow2"), ("10.0.10.3", "cirros-0.6.2.qcow2")})

    def test_borrado_inteligente_al_eliminar_el_slice(self):
        drv, hs, alloc = DriverLinux(), hosts(), VLANAllocator(3000, 3099)
        s = desplegar_slice_existente(ex1(), hs, drv, alloc, RR)
        self.assertGreater(len(drv.imagenes_simuladas), 0)
        eliminar_slice(s, hs, drv, alloc)
        self.assertEqual(len(drv.imagenes_simuladas), 0)       # ninguna copia queda en los workers
        self.assertEqual(len(drv.vms_simuladas), 0)

    def test_no_borra_la_imagen_si_otra_vm_la_sigue_usando(self):
        drv, hs, alloc = DriverLinux(), hosts(), VLANAllocator(3000, 3099)
        s = desplegar_slice_existente(ex1(), hs, drv, alloc, RR)
        uri1 = hs[0].uri_libvirt
        drv.eliminar_vm(uri1, "demo-ex1-vm1")                  # VM4 sigue usando ubuntu en worker1
        self.assertFalse(drv.liberar_imagen_si_huerfana(uri1, "ubuntu-22.04.qcow2"))
        self.assertIn((uri1, "ubuntu-22.04.qcow2"), drv.imagenes_simuladas)
        drv.eliminar_vm(uri1, "demo-ex1-vm4")                  # ahora ya nadie la usa
        self.assertTrue(drv.liberar_imagen_si_huerfana(uri1, "ubuntu-22.04.qcow2"))

    def test_no_borra_imagenes_que_el_orquestador_no_copio(self):
        drv, hs = DriverLinux(), hosts()
        uri = hs[0].uri_libvirt
        drv.imagenes_simuladas.add((uri, "ubuntu-22.04.qcow2"))     # ya estaba en el worker
        self.assertFalse(drv.asegurar_imagen(uri, "ubuntu-22.04.qcow2"))   # no la copia
        self.assertFalse(drv.liberar_imagen_si_huerfana(uri, "ubuntu-22.04.qcow2"))  # y no la borra
        self.assertIn((uri, "ubuntu-22.04.qcow2"), drv.imagenes_simuladas)


if __name__ == "__main__":
    unittest.main()
