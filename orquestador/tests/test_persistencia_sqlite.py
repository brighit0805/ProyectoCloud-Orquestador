"""Pruebas de la persistencia SQLite (HLD dominio C): ida y vuelta, escrituras
solo de lo que cambia, concurrencia entre procesos, migración desde el JSON
anterior y consulta SQL de los slices desplegados."""

import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from slice_orchestrator.persistencia_sqlite import AlmacenSQLite, es_ruta_sqlite
from slice_orchestrator.servicio_slices import ErrorServicio, ServicioSlices
from tests.test_r1b import spec_ex1, spec_simple


def _crear_en_proceso(ruta, usuario, nombre, cola):
    svc = ServicioSlices(ruta_estado=ruta)
    cola.put(svc.crear_slice(usuario, spec_simple(nombre))["id"])


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = str(Path(self.dir.name) / "estado.db")


class TestRutas(unittest.TestCase):
    def test_extensiones(self):
        self.assertTrue(es_ruta_sqlite("x.db"))
        self.assertTrue(es_ruta_sqlite(Path("x.sqlite3")))
        self.assertFalse(es_ruta_sqlite("x.json"))
        self.assertFalse(es_ruta_sqlite(None))


class TestRoundTrip(_Base):
    def test_estado_sobrevive_al_reinicio(self):
        a = ServicioSlices(ruta_estado=self.db)
        s = a.crear_slice("ana", spec_ex1("uno"), desplegar=True)
        b = ServicioSlices(ruta_estado=self.db)
        self.assertEqual([x["id"] for x in b.listar_slices("ana")], [s["id"]])
        self.assertEqual(b.listar_slices("ana")[0]["estado"], "activo")
        # el contador continúa, no reutiliza IDs
        n = b.crear_slice("ana", spec_simple("dos"))
        self.assertNotEqual(n["id"], s["id"])

    def test_eliminar_se_persiste(self):
        a = ServicioSlices(ruta_estado=self.db)
        s = a.crear_slice("ana", spec_simple("x"))
        a.eliminar_slice("ana", s["id"])
        self.assertEqual(ServicioSlices(ruta_estado=self.db).listar_slices("ana"), [])

    def test_base_corrupta_da_error_claro(self):
        Path(self.db).write_bytes(b"esto no es sqlite" * 100)
        with self.assertRaises(ErrorServicio):
            ServicioSlices(ruta_estado=self.db)


class TestConsultasSQL(_Base):
    def test_vista_de_desplegados_y_tablas_relacionales(self):
        svc = ServicioSlices(ruta_estado=self.db)
        svc.crear_slice("ana", spec_simple("borrador"))
        d = svc.crear_slice("ana", spec_ex1("activo1"), desplegar=True)
        con = sqlite3.connect(self.db)
        self.addCleanup(con.close)
        filas = con.execute("SELECT * FROM v_slices_desplegados").fetchall()
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0][0], d["id"])
        n = con.execute("SELECT COUNT(*) FROM nodos WHERE slice_id=?", (d["id"],)).fetchone()[0]
        self.assertEqual(n, 6)
        self.assertGreater(
            con.execute("SELECT COUNT(*) FROM enlaces WHERE slice_id=?", (d["id"],)).fetchone()[0], 0)

    def test_borrar_slice_borra_nodos_y_enlaces(self):
        svc = ServicioSlices(ruta_estado=self.db)
        s = svc.crear_slice("ana", spec_ex1("x"))
        svc.eliminar_slice("ana", s["id"])
        con = sqlite3.connect(self.db)
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM nodos").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM enlaces").fetchone()[0], 0)


class TestEscrituraDiferencial(_Base):
    def test_no_pisa_filas_ajenas(self):
        a = ServicioSlices(ruta_estado=self.db)
        b = ServicioSlices(ruta_estado=self.db)
        sa = a.crear_slice("ana", spec_simple("de-a"))
        sb = b.crear_slice("beto", spec_simple("de-b"))
        self.assertNotEqual(sa["id"], sb["id"])
        # a todavía no ha visto el slice de b; al guardar otro cambio no debe borrarlo
        a.crear_slice("ana", spec_simple("de-a-2"))
        ids = {x["id"] for x in ServicioSlices(ruta_estado=self.db).listar_todos_slices()}
        self.assertEqual(len(ids), 3)
        self.assertIn(sb["id"], ids)

    def test_ve_cambios_de_otro_proceso_sin_reiniciar(self):
        a = ServicioSlices(ruta_estado=self.db)
        b = ServicioSlices(ruta_estado=self.db)
        sb = b.crear_slice("ana", spec_simple("creado-por-b"))
        self.assertIn(sb["id"], [x["id"] for x in a.listar_slices("ana")])
        b.eliminar_slice("ana", sb["id"])
        self.assertEqual(a.listar_slices("ana"), [])


class TestConcurrencia(_Base):
    def test_ids_unicos_entre_procesos(self):
        ServicioSlices(ruta_estado=self.db)  # crea el esquema
        cola = multiprocessing.Queue()
        procs = [multiprocessing.Process(target=_crear_en_proceso,
                                         args=(self.db, "ana", f"s{i}", cola))
                 for i in range(6)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            self.assertEqual(p.exitcode, 0)
        ids = [cola.get(timeout=5) for _ in procs]
        self.assertEqual(len(set(ids)), 6)
        self.assertEqual(len(ServicioSlices(ruta_estado=self.db).listar_slices("ana")), 6)


class TestMigracion(_Base):
    def test_migra_json_anterior_una_sola_vez(self):
        ruta_json = Path(self.dir.name) / "estado.json"
        viejo = ServicioSlices(ruta_estado=str(ruta_json))
        s = viejo.crear_slice("ana", spec_ex1("legado"), desplegar=True)
        original = ruta_json.read_text()
        nuevo = ServicioSlices(ruta_estado=self.db)
        self.assertEqual([x["id"] for x in nuevo.listar_slices("ana")], [s["id"]])
        self.assertEqual(ruta_json.read_text(), original)  # el JSON no se toca
        # segundo arranque: no duplica ni resucita lo borrado
        nuevo.eliminar_slice("ana", s["id"])
        self.assertEqual(ServicioSlices(ruta_estado=self.db).listar_slices("ana"), [])

    def test_no_migra_si_la_base_ya_tiene_datos(self):
        a = ServicioSlices(ruta_estado=self.db)
        a.crear_slice("ana", spec_simple("ya-en-db"))
        ruta_json = Path(self.dir.name) / "estado.json"
        ServicioSlices(ruta_estado=str(ruta_json)).crear_slice("ana", spec_simple("del-json"))
        nombres = [x["nombre"] for x in ServicioSlices(ruta_estado=self.db).listar_slices("ana")]
        self.assertEqual(nombres, ["ya-en-db"])


class TestJsonSigueFuncionando(_Base):
    def test_backend_json_intacto(self):
        ruta = str(Path(self.dir.name) / "e.json")
        ServicioSlices(ruta_estado=ruta).crear_slice("ana", spec_simple("j"))
        self.assertEqual(json.loads(Path(ruta).read_text())["version"], 1)


if __name__ == "__main__":
    unittest.main()
