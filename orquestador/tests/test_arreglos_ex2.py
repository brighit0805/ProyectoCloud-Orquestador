"""Arreglos rápidos para el guion de validación Ex2: nombres con mayúsculas y
guion bajo, imágenes .img, borrado de imágenes y zona Linux server1..server4."""

import tempfile
import unittest
from pathlib import Path

import config_cluster
from slice_orchestrator.servicio_slices import ErrorServicio, ServicioSlices
from tests.test_driver_openstack import driver_simulado
from tests.test_r1b import spec_simple

QCOW2 = b"QFI\xfb" + b"\x00" * 64


class _DriverRealFalso:
    modo = "real"

    def __init__(self):
        self.subidas, self.borradas = [], []

    def subir_imagen(self, nombre, ruta):
        self.subidas.append(nombre)
        return f"id-{nombre}"

    def borrar_imagen(self, nombre, imagen_id=None):
        self.borradas.append((nombre, imagen_id))
        return True


class TestNombres(unittest.TestCase):
    def test_nombre_de_examen_valido(self):
        svc = ServicioSlices()
        for n in ("Ex2_G02_test", "Ex2_G02_test_2"):
            self.assertEqual(svc.crear_slice("ana", spec_simple(n))["nombre"], n)

    def test_nombres_invalidos_siguen_rechazados(self):
        svc = ServicioSlices()
        for n in ("", "-x", "con espacio", "a" * 31, "pun.to"):
            with self.assertRaises(ErrorServicio):
                svc.crear_slice("ana", spec_simple(n))


class TestImagenes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.img = Path(self.tmp.name) / "ubuntu-24.04-minimal-cloudimg-amd64.img"
        self.img.write_bytes(QCOW2)
        self.drv = _DriverRealFalso()
        self.svc = ServicioSlices(driver=self.drv)

    def test_importa_img_y_la_sube(self):
        r = self.svc.importar_imagen("ana", self.img.name, str(self.img))
        self.assertTrue(r["subida_a_plataforma"])
        self.assertEqual(self.drv.subidas, [self.img.name])

    def test_extension_no_valida_se_rechaza(self):
        with self.assertRaises(ErrorServicio):
            self.svc.importar_imagen("ana", "x.iso", str(self.img))

    def test_eliminar_borra_de_catalogo_y_plataforma(self):
        self.svc.importar_imagen("ana", self.img.name, str(self.img))
        r = self.svc.eliminar_imagen("ana", self.img.name)
        self.assertTrue(r["borrada_de_plataforma"])
        self.assertEqual(self.drv.borradas, [(self.img.name, f"id-{self.img.name}")])
        self.assertNotIn(self.img.name, [i["nombre"] for i in self.svc.listar_imagenes()])

    def test_no_borra_catalogo_base_ni_inexistente(self):
        for nombre in ("cirros-0.6.2.qcow2", "no-existe.img"):
            with self.assertRaises(ErrorServicio):
                self.svc.eliminar_imagen("ana", nombre)

    def test_no_borra_imagen_en_uso(self):
        self.svc.importar_imagen("ana", self.img.name, str(self.img))
        self.svc.crear_slice("ana", spec_simple(
            "usa-img", capacidad={"vm1": {"imagen": self.img.name}}))
        with self.assertRaises(ErrorServicio):
            self.svc.eliminar_imagen("ana", self.img.name)

    def test_driver_openstack_borrar_imagen(self):
        d = driver_simulado()
        d.sesion.imagenes["img-x"] = {"id": "img-x", "name": "mi.img"}
        self.assertTrue(d.borrar_imagen("mi.img"))             # por nombre
        self.assertNotIn("img-x", d.sesion.imagenes)
        self.assertFalse(d.borrar_imagen("mi.img"))            # idempotente


class TestZonaLinux(unittest.TestCase):
    def test_zona_agrupa_server1_a_server4(self):
        self.assertEqual(sorted(config_cluster.WORKERS),
                         ["server1", "server2", "server3", "server4"])
        self.assertTrue(config_cluster.ZONA_LINUX)


if __name__ == "__main__":
    unittest.main()
