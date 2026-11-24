"""
test_cola_trabajos.py
---------------------
R1: cola de trabajos para varios usuarios concurrentes (gestor de
colas/tareas de la arquitectura). Cubre la cola en sí, su uso desde el
servicio de slices y los endpoints asíncronos de la API HTTP.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slice_orchestrator import api_http
from slice_orchestrator.cola_trabajos import ColaTrabajos, ErrorCola
from slice_orchestrator.control_acceso import GestorUsuarios
from slice_orchestrator.driver_linux import DriverLinux
from slice_orchestrator.servicio_slices import ErrorServicio, NoEncontrado, ServicioSlices


class TestCola(unittest.TestCase):
    def setUp(self):
        self.orden = []
        self.soltar = threading.Event()

        def ejecutar(t):
            self.soltar.wait(5)
            if t.parametros.get("fallar"):
                raise RuntimeError("Nova confirmó ERROR")
            self.orden.append(t.slice_id)
            return {"slice": t.slice_id}
        self.cola = ColaTrabajos({"desplegar": ejecutar})

    def tearDown(self):
        self.soltar.set()
        self.cola.detener()

    def test_responde_al_instante_y_ejecuta_en_orden_de_llegada(self):
        a = self.cola.encolar("desplegar", "ana", "s1")
        b = self.cola.encolar("desplegar", "bob", "s2")
        c = self.cola.encolar("desplegar", "ana", "s3")
        self.assertEqual(a["estado"], "pendiente")
        time.sleep(0.05)
        # s1 ya está en curso; s2 y s3 esperan en ese orden.
        self.assertEqual(self.cola.obtener(a["id"])["estado"], "en_curso")
        self.assertEqual([self.cola.obtener(b["id"])["posicion"],
                          self.cola.obtener(c["id"])["posicion"]], [1, 2])
        self.soltar.set()
        for t in (a, b, c):
            self.assertEqual(self.cola.esperar(t["id"], 5)["estado"], "completado")
        self.assertEqual(self.orden, ["s1", "s2", "s3"])

    def test_resultado_y_tiempos(self):
        self.soltar.set()
        t = self.cola.esperar(self.cola.encolar("desplegar", "ana", "s1")["id"], 5)
        self.assertEqual(t["resultado"], {"slice": "s1"})
        self.assertLessEqual(t["creado"], t["iniciado"])
        self.assertLessEqual(t["iniciado"], t["terminado"])
        self.assertIsNone(t["posicion"])

    def test_un_fallo_queda_registrado_y_la_cola_sigue(self):
        self.soltar.set()
        malo = self.cola.encolar("desplegar", "ana", "s1", fallar=True)
        bueno = self.cola.encolar("desplegar", "bob", "s2")
        r = self.cola.esperar(malo["id"], 5)
        self.assertEqual((r["estado"], r["error"]), ("fallido", "Nova confirmó ERROR"))
        self.assertEqual(self.cola.esperar(bueno["id"], 5)["estado"], "completado")

    def test_no_dos_trabajos_activos_sobre_el_mismo_slice(self):
        self.cola.encolar("desplegar", "ana", "s1")
        with self.assertRaises(ErrorCola):
            self.cola.encolar("desplegar", "ana", "s1")

    def test_limite_de_pendientes_por_usuario(self):
        cola = ColaTrabajos({"desplegar": lambda t: self.soltar.wait(5)},
                            max_pendientes_por_usuario=2)
        try:
            cola.encolar("desplegar", "ana", "s1")
            cola.encolar("desplegar", "ana", "s2")
            with self.assertRaises(ErrorCola):
                cola.encolar("desplegar", "ana", "s3")
            cola.encolar("desplegar", "bob", "s4")        # otro usuario sí puede
        finally:
            self.soltar.set()
            cola.detener()

    def test_tipo_desconocido(self):
        with self.assertRaises(ErrorCola):
            self.cola.encolar("reiniciar", "ana", "s1")

    def test_esperar_con_timeout(self):
        t = self.cola.encolar("desplegar", "ana", "s1")
        with self.assertRaises(TimeoutError):
            self.cola.esperar(t["id"], 0.05)

    def test_listar_por_usuario_mas_reciente_primero(self):
        self.cola.encolar("desplegar", "ana", "s1")
        self.cola.encolar("desplegar", "bob", "s2")
        self.cola.encolar("desplegar", "ana", "s3")
        self.assertEqual([t["slice_id"] for t in self.cola.listar("ana")], ["s3", "s1"])
        self.assertEqual(len(self.cola.listar()), 3)

    def test_al_menos_un_trabajador(self):
        with self.assertRaises(ValueError):
            ColaTrabajos({}, trabajadores=0, iniciar=False)


def spec(nombre):
    return {"nombre": nombre, "topologia": {"base": {"tipo": "anillo", "nodos": 3}}}


class TestServicio(unittest.TestCase):
    def setUp(self):
        self.svc = ServicioSlices(driver=DriverLinux())

    def test_despliegue_encolado(self):
        d = self.svc.crear_slice("ana", spec("a"))
        t = self.svc.encolar_despliegue("ana", d["id"], "round_robin")
        self.assertEqual(t["tipo"], "desplegar")
        r = self.svc.esperar_trabajo(t["id"], 10)
        self.assertEqual(r["estado"], "completado")
        self.assertEqual(self.svc.ver_slice("ana", d["id"])["estado"], "activo")

    def test_eliminacion_encolada(self):
        d = self.svc.crear_slice("ana", spec("a"), desplegar=True, estrategia="round_robin")
        t = self.svc.encolar_eliminacion("ana", d["id"])
        self.assertEqual(self.svc.esperar_trabajo(t["id"], 10)["estado"], "completado")
        self.assertEqual(self.svc.listar_slices("ana"), [])

    def test_valida_antes_de_encolar(self):
        d = self.svc.crear_slice("ana", spec("a"), desplegar=True, estrategia="round_robin")
        with self.assertRaises(ErrorServicio):
            self.svc.encolar_despliegue("ana", d["id"])         # ya está activo
        with self.assertRaises(NoEncontrado):
            self.svc.encolar_despliegue("bob", d["id"])         # no es suyo

    def test_fallo_del_despliegue_queda_en_el_trabajo(self):
        svc = ServicioSlices(driver=DriverLinux(fallar_en_operacion=1, fallas_consecutivas=50))
        d = svc.crear_slice("ana", spec("a"))
        r = svc.esperar_trabajo(svc.encolar_despliegue("ana", d["id"], "round_robin")["id"], 10)
        self.assertEqual(r["estado"], "fallido")
        self.assertIn("No se pudo desplegar", r["error"])
        self.assertEqual(svc.ver_slice("ana", d["id"])["estado"], "error")

    def test_solo_el_dueno_o_admin_ven_el_trabajo(self):
        d = self.svc.crear_slice("ana", spec("a"))
        t = self.svc.encolar_despliegue("ana", d["id"], "round_robin")
        with self.assertRaises(NoEncontrado):
            self.svc.ver_trabajo("bob", t["id"])
        self.assertEqual(self.svc.ver_trabajo("brighit", t["id"], admin=True)["id"], t["id"])
        self.assertEqual(len(self.svc.listar_trabajos("bob")), 0)
        self.assertEqual(len(self.svc.listar_trabajos("bob", todos=True)), 1)

    def test_admin_elimina_slice_ajeno_por_la_cola(self):
        d = self.svc.crear_slice("ana", spec("a"), desplegar=True, estrategia="round_robin")
        t = self.svc.encolar_eliminacion("brighit", d["id"], admin=True)
        self.assertEqual(self.svc.esperar_trabajo(t["id"], 10)["estado"], "completado")
        self.assertEqual(self.svc.ver_trabajo("ana", t["id"])["id"], t["id"])   # el dueño también lo ve

    def test_varios_usuarios_a_la_vez(self):
        """Seis usuarios piden despliegues desde hilos distintos al mismo tiempo."""
        ids, errores = [], []

        def pedir(u):
            try:
                d = self.svc.crear_slice(u, spec(f"s-{u}"))
                ids.append(self.svc.encolar_despliegue(u, d["id"], "round_robin")["id"])
            except Exception as exc:      # noqa: BLE001
                errores.append(exc)
        hilos = [threading.Thread(target=pedir, args=(f"u{i}",)) for i in range(6)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        self.assertEqual(errores, [])
        estados = [self.svc.esperar_trabajo(i, 20)["estado"] for i in ids]
        self.assertEqual(estados, ["completado"] * 6)


def _peticion(metodo, url, token=None, cuerpo=None):
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, method=metodo)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@mock.patch.dict(os.environ, {"ORQ_API_SECRET": "secreto-de-prueba-para-los-tests"})
class TestApiAsincrona(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        usuarios = os.path.join(self.dir.name, "usuarios.json")
        gestor = GestorUsuarios(usuarios)
        gestor.agregar("brighit", "clave-admin-1", "admin")
        gestor.agregar("ana", "clave-usuario-1", "usuario")
        self.servidor = api_http.crear_servidor(
            driver_nombre="linux", ruta_estado=os.path.join(self.dir.name, "estado.json"),
            ruta_usuarios=usuarios, puerto=0, host="127.0.0.1")
        self.base = f"http://127.0.0.1:{self.servidor.server_address[1]}"
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()

    def tearDown(self):
        self.servidor.shutdown()
        self.servidor.server_close()
        self.dir.cleanup()

    def token(self, u, c):
        return _peticion("POST", f"{self.base}/login", cuerpo={"usuario": u, "contrasena": c})[1]["token"]

    def esperar(self, tok, trabajo_id):
        for _ in range(200):
            status, t = _peticion("GET", f"{self.base}/trabajos/{trabajo_id}", token=tok)
            self.assertEqual(status, 200, t)
            if t["estado"] in ("completado", "fallido"):
                return t
            time.sleep(0.05)
        self.fail("el trabajo no terminó")

    def test_crear_y_desplegar_asincrono(self):
        tok = self.token("ana", "clave-usuario-1")
        status, r = _peticion("POST", f"{self.base}/slices", token=tok, cuerpo={
            "nombre": "async1", "topologia": {"base": {"tipo": "lineal", "nodos": 2}},
            "desplegar": True, "asincrono": True, "estrategia": "round_robin"})
        self.assertEqual(status, 202, r)
        self.assertEqual(r["slice"]["estado"], "borrador")
        self.assertEqual(self.esperar(tok, r["trabajo"]["id"])["estado"], "completado")
        _, s = _peticion("GET", f"{self.base}/slices/{r['slice']['id']}", token=tok)
        self.assertEqual(s["estado"], "activo")

    def test_desplegar_y_eliminar_por_la_cola(self):
        tok = self.token("ana", "clave-usuario-1")
        _, d = _peticion("POST", f"{self.base}/slices", token=tok, cuerpo={
            "nombre": "async2", "topologia": {"base": {"tipo": "lineal", "nodos": 2}}})
        status, t = _peticion("POST", f"{self.base}/slices/{d['id']}/desplegar", token=tok,
                              cuerpo={"estrategia": "round_robin"})
        self.assertEqual(status, 202, t)
        self.assertEqual(self.esperar(tok, t["id"])["estado"], "completado")
        status, t = _peticion("DELETE", f"{self.base}/slices/{d['id']}?asincrono=1", token=tok)
        self.assertEqual(status, 202, t)
        self.assertEqual(self.esperar(tok, t["id"])["estado"], "completado")
        status, lista = _peticion("GET", f"{self.base}/trabajos", token=tok)
        self.assertEqual((status, len(lista)), (200, 2))

    def test_trabajo_ajeno_da_404(self):
        tok_ana = self.token("ana", "clave-usuario-1")
        _, d = _peticion("POST", f"{self.base}/slices", token=tok_ana, cuerpo={
            "nombre": "async3", "topologia": {"base": {"tipo": "lineal", "nodos": 2}}})
        _, t = _peticion("POST", f"{self.base}/slices/{d['id']}/desplegar", token=tok_ana,
                         cuerpo={"estrategia": "round_robin"})
        GestorUsuarios(os.path.join(self.dir.name, "usuarios.json")).agregar("bob", "clave-2", "usuario")
        status, _ = _peticion("GET", f"{self.base}/trabajos/{t['id']}", token=self.token("bob", "clave-2"))
        self.assertEqual(status, 404)
        tok_admin = self.token("brighit", "clave-admin-1")
        status, todos = _peticion("GET", f"{self.base}/trabajos?todos=1", token=tok_admin)
        self.assertEqual((status, len(todos)), (200, 1))


if __name__ == "__main__":
    unittest.main()
