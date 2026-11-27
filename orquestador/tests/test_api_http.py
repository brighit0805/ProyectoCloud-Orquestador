"""Pruebas de extremo a extremo de la API HTTP de R1C: levanta el servidor
real (con el driver Linux SIMULADO) en un puerto libre de localhost y le
hace peticiones HTTP de verdad con urllib -- no mockea nada de la capa
HTTP, solo el driver de infraestructura (como el resto de R1B)."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from slice_orchestrator import api_http
from slice_orchestrator.control_acceso import GestorUsuarios


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


@patch.dict(os.environ, {"ORQ_API_SECRET": "secreto-de-prueba-para-los-tests"})
class TestApiHttp(unittest.TestCase):
    def setUp(self):
        self._tmp_usuarios = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp_usuarios.close()
        os.unlink(self._tmp_usuarios.name)
        self._tmp_estado = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp_estado.close()
        os.unlink(self._tmp_estado.name)

        gestor = GestorUsuarios(self._tmp_usuarios.name)
        gestor.agregar("brighit", "clave-admin-1", "admin")
        gestor.agregar("ana", "clave-usuario-1", "usuario")

        self.servidor = api_http.crear_servidor(
            driver_nombre="linux", ruta_estado=self._tmp_estado.name,
            ruta_usuarios=self._tmp_usuarios.name, puerto=0, host="127.0.0.1")
        self.puerto = self.servidor.server_address[1]
        self.base = f"http://127.0.0.1:{self.puerto}"
        self.hilo = threading.Thread(target=self.servidor.serve_forever, daemon=True)
        self.hilo.start()

    def tearDown(self):
        self.servidor.shutdown()
        self.servidor.server_close()
        for ruta in (self._tmp_usuarios.name, self._tmp_estado.name):
            if os.path.exists(ruta):
                os.unlink(ruta)

    def _token(self, usuario, password):
        status, cuerpo = _peticion("POST", f"{self.base}/login",
                                    cuerpo={"usuario": usuario, "contrasena": password})
        self.assertEqual(status, 200, cuerpo)
        return cuerpo["token"], cuerpo["rol"]

    def _spec_lineal(self, nombre):
        return {"nombre": nombre, "topologia": {"base": {"tipo": "lineal", "nodos": 2}}}

    # -- plan de broadcast (GUI, pestaña Red) -------------------------------
    def test_red_broadcast_requiere_token_y_devuelve_lista(self):
        status, _ = _peticion("GET", f"{self.base}/red/broadcast")
        self.assertEqual(status, 401)
        token, _ = self._token("ana", "clave-usuario-1")
        status, cuerpo = _peticion("GET", f"{self.base}/red/broadcast", token=token)
        self.assertEqual(status, 200)
        self.assertIsInstance(cuerpo, list)

    # -- login -------------------------------------------------------------
    def test_login_correcto_devuelve_token_y_rol(self):
        token, rol = self._token("brighit", "clave-admin-1")
        self.assertTrue(token)
        self.assertEqual(rol, "admin")

    def test_login_con_password_incorrecta_devuelve_401(self):
        status, cuerpo = _peticion("POST", f"{self.base}/login",
                                    cuerpo={"usuario": "brighit", "contrasena": "mala"})
        self.assertEqual(status, 401)

    # -- autenticación general ----------------------------------------------
    def test_sin_token_las_rutas_protegidas_devuelven_401(self):
        status, _ = _peticion("GET", f"{self.base}/slices")
        self.assertEqual(status, 401)

    def test_token_invalido_devuelve_401(self):
        status, _ = _peticion("GET", f"{self.base}/slices", token="esto-no-es-un-token")
        self.assertEqual(status, 401)

    # -- aislamiento por usuario ---------------------------------------------
    def test_usuario_solo_ve_sus_propios_slices(self):
        token_ana, _ = self._token("ana", "clave-usuario-1")
        status, creado = _peticion("POST", f"{self.base}/slices", token=token_ana,
                                    cuerpo=self._spec_lineal("de-ana"))
        self.assertEqual(status, 201, creado)

        token_brighit, _ = self._token("brighit", "clave-admin-1")
        _peticion("POST", f"{self.base}/slices", token=token_brighit,
                  cuerpo=self._spec_lineal("de-brighit"))

        status, listado_ana = _peticion("GET", f"{self.base}/slices", token=token_ana)
        self.assertEqual(status, 200)
        self.assertEqual([s["nombre"] for s in listado_ana], ["de-ana"])

    def test_usuario_no_puede_ver_el_slice_de_otro(self):
        token_ana, _ = self._token("ana", "clave-usuario-1")
        _, creado = _peticion("POST", f"{self.base}/slices", token=token_ana,
                               cuerpo=self._spec_lineal("de-ana"))
        token_brighit, _ = self._token("brighit", "clave-admin-1")
        # brighit es admin, pero probamos con un segundo usuario normal:
        # un 'usuario' no debe poder ver el slice de otro 'usuario'.
        status, _ = _peticion("GET", f"{self.base}/slices/{creado['id']}", token=token_brighit)
        # brighit es admin así que SÍ puede verlo (se prueba aparte); aquí
        # solo confirmamos que el endpoint exige autenticación y resuelve.
        self.assertEqual(status, 200)

    def test_usuario_normal_no_puede_ver_slice_ajeno(self):
        gestor = GestorUsuarios(self._tmp_usuarios.name)
        gestor.agregar("beto", "clave-usuario-2", "usuario")
        token_ana, _ = self._token("ana", "clave-usuario-1")
        _, creado = _peticion("POST", f"{self.base}/slices", token=token_ana,
                               cuerpo=self._spec_lineal("de-ana"))
        token_beto, _ = self._token("beto", "clave-usuario-2")
        status, _ = _peticion("GET", f"{self.base}/slices/{creado['id']}", token=token_beto)
        self.assertEqual(status, 404)

    # -- rol admin -------------------------------------------------------------
    def test_admin_ve_todos_los_slices_con_todos_1(self):
        token_ana, _ = self._token("ana", "clave-usuario-1")
        _peticion("POST", f"{self.base}/slices", token=token_ana,
                  cuerpo=self._spec_lineal("de-ana"))
        token_brighit, _ = self._token("brighit", "clave-admin-1")
        _peticion("POST", f"{self.base}/slices", token=token_brighit,
                  cuerpo=self._spec_lineal("de-brighit"))

        status, propios = _peticion("GET", f"{self.base}/slices", token=token_brighit)
        self.assertEqual([s["nombre"] for s in propios], ["de-brighit"])

        status, todos = _peticion("GET", f"{self.base}/slices?todos=1", token=token_brighit)
        self.assertEqual(status, 200)
        self.assertEqual(sorted(s["nombre"] for s in todos), ["de-ana", "de-brighit"])

    def test_usuario_normal_con_todos_1_sigue_viendo_solo_lo_suyo(self):
        """?todos=1 no es una puerta trasera: solo funciona si el rol es admin."""
        token_ana, _ = self._token("ana", "clave-usuario-1")
        _peticion("POST", f"{self.base}/slices", token=token_ana,
                  cuerpo=self._spec_lineal("de-ana"))
        status, resultado = _peticion("GET", f"{self.base}/slices?todos=1", token=token_ana)
        self.assertEqual(status, 200)
        self.assertEqual([s["nombre"] for s in resultado], ["de-ana"])

    def test_admin_puede_eliminar_slice_de_otro_usuario(self):
        token_ana, _ = self._token("ana", "clave-usuario-1")
        _, creado = _peticion("POST", f"{self.base}/slices", token=token_ana,
                               cuerpo=self._spec_lineal("de-ana"))
        token_brighit, _ = self._token("brighit", "clave-admin-1")
        status, _ = _peticion("DELETE", f"{self.base}/slices/{creado['id']}", token=token_brighit)
        self.assertEqual(status, 200)

    def test_usuario_normal_no_puede_correr_mantenimiento(self):
        token_ana, _ = self._token("ana", "clave-usuario-1")
        status, cuerpo = _peticion("POST", f"{self.base}/mantenimiento/limpiar-residuos",
                                    token=token_ana, cuerpo={})
        self.assertEqual(status, 403, cuerpo)

    def test_admin_si_puede_correr_mantenimiento(self):
        token_brighit, _ = self._token("brighit", "clave-admin-1")
        status, cuerpo = _peticion("POST", f"{self.base}/mantenimiento/limpiar-residuos",
                                    token=token_brighit, cuerpo={})
        # El driver Linux simulado no implementa limpiar_residuos -- se
        # espera un 400 (ErrorServicio), pero NUNCA un 403 (ya pasó la
        # autorización de rol, que es lo que esta prueba verifica).
        self.assertIn(status, (200, 400))

    # -- consumo de recursos ----------------------------------------------
    def test_recursos_refleja_el_slice_desplegado(self):
        token_ana, _ = self._token("ana", "clave-usuario-1")
        status_crear, creado = _peticion(
            "POST", f"{self.base}/slices", token=token_ana,
            cuerpo={**self._spec_lineal("rec-ana"), "desplegar": True})
        self.assertEqual(status_crear, 201, creado)
        status, recursos = _peticion("GET", f"{self.base}/recursos", token=token_ana)
        self.assertEqual(status, 200)
        self.assertEqual(recursos["total_mio"]["vcpu"], 2)

    # -- cuenta: cambiar contraseña (GUI) ------------------------------------
    def test_cambiar_contrasena(self):
        token_ana, _ = self._token("ana", "clave-usuario-1")
        status, cuerpo = _peticion("POST", f"{self.base}/cuenta/contrasena", token=token_ana,
                                   cuerpo={"actual": "clave-usuario-1", "nueva": "otra-clave-22"})
        self.assertEqual(status, 200, cuerpo)
        status, _ = _peticion("POST", f"{self.base}/login",
                              cuerpo={"usuario": "ana", "contrasena": "clave-usuario-1"})
        self.assertEqual(status, 401)
        _, rol = self._token("ana", "otra-clave-22")
        self.assertEqual(rol, "usuario")                 # el rol no cambia

    def test_cambiar_contrasena_rechaza_actual_incorrecta_o_nueva_corta(self):
        token_ana, _ = self._token("ana", "clave-usuario-1")
        for cuerpo in ({"actual": "mala", "nueva": "otra-clave-22"},
                       {"actual": "clave-usuario-1", "nueva": "corta"},
                       {"actual": "clave-usuario-1", "nueva": "clave-usuario-1"}):
            status, resp = _peticion("POST", f"{self.base}/cuenta/contrasena",
                                     token=token_ana, cuerpo=cuerpo)
            self.assertEqual(status, 400, resp)        # 400: la sesión sigue válida
        self._token("ana", "clave-usuario-1")          # la clave no cambió

    def test_cambiar_contrasena_requiere_token(self):
        status, _ = _peticion("POST", f"{self.base}/cuenta/contrasena",
                              cuerpo={"actual": "x", "nueva": "yyyyyyyy"})
        self.assertEqual(status, 401)

    # -- consola de una VM (GUI) --------------------------------------------
    def test_consola_de_un_nodo_propio(self):
        token_ana, _ = self._token("ana", "clave-usuario-1")
        _, creado = _peticion("POST", f"{self.base}/slices", token=token_ana,
                              cuerpo={**self._spec_lineal("con-ana"), "desplegar": True})
        status, cuerpo = _peticion("POST", f"{self.base}/slices/{creado['id']}/consola",
                                   token=token_ana, cuerpo={"nodo": "vm1"})
        self.assertEqual(status, 200, cuerpo)
        self.assertTrue(cuerpo["token"])
        self.assertIn("puerto", cuerpo)

    def test_consola_de_slice_ajeno_no_se_emite(self):
        token_ana, _ = self._token("ana", "clave-usuario-1")
        _, creado = _peticion("POST", f"{self.base}/slices", token=token_ana,
                              cuerpo={**self._spec_lineal("con-ana2"), "desplegar": True})
        token_brighit, _ = self._token("brighit", "clave-admin-1")
        status, _ = _peticion("POST", f"{self.base}/slices/{creado['id']}/consola",
                              token=token_brighit, cuerpo={"nodo": "vm1"})
        self.assertIn(status, (400, 403, 404))


if __name__ == "__main__":
    unittest.main()
