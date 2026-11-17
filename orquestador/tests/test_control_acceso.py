"""Pruebas de R1C: hashing de contraseñas, tokens firmados, roles, y el
gestor de usuarios (control_acceso.py). Todo sin tocar ningún driver."""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from slice_orchestrator.control_acceso import (
    ErrorAcceso,
    GestorUsuarios,
    exigir_rol,
    generar_token,
    hash_password,
    verificar_password,
    verificar_token,
)


class TestHashPassword(unittest.TestCase):
    def test_misma_password_y_sal_dan_el_mismo_hash(self):
        sal, h1 = hash_password("clave-secreta-123")
        _, h2 = hash_password("clave-secreta-123", sal)
        self.assertEqual(h1, h2)

    def test_misma_password_con_sal_distinta_da_hash_distinto(self):
        sal1, h1 = hash_password("clave-secreta-123")
        sal2, h2 = hash_password("clave-secreta-123")
        self.assertNotEqual(sal1, sal2)
        self.assertNotEqual(h1, h2)

    def test_verificar_password_correcta_e_incorrecta(self):
        sal, h = hash_password("clave-secreta-123")
        self.assertTrue(verificar_password("clave-secreta-123", sal, h))
        self.assertFalse(verificar_password("otra-clave", sal, h))


@patch.dict(os.environ, {"ORQ_API_SECRET": "secreto-de-prueba-no-real"})
class TestTokens(unittest.TestCase):
    def test_token_valido_se_puede_verificar(self):
        token = generar_token("brighit", "admin")
        datos = verificar_token(token)
        self.assertEqual(datos, {"usuario": "brighit", "rol": "admin"})

    def test_token_expirado_se_rechaza(self):
        token = generar_token("brighit", "usuario", ttl_seg=-1)
        with self.assertRaises(ErrorAcceso):
            verificar_token(token)

    def test_token_con_firma_alterada_se_rechaza(self):
        token = generar_token("brighit", "usuario")
        header, payload, firma = token.split(".")
        alterado = f"{header}.{payload}.{firma[:-2]}xx"
        with self.assertRaises(ErrorAcceso):
            verificar_token(alterado)

    def test_token_con_payload_alterado_se_rechaza(self):
        """Si alguien fabrica un payload con rol 'admin' sin conocer el
        secreto, la firma original (de otro payload) nunca va a coincidir
        -- no se puede "subir" de usuario a admin con un token ajeno."""
        import base64
        import json as _json

        token_usuario = generar_token("mallory", "usuario")
        header, _payload_original, firma = token_usuario.split(".")
        payload_admin = base64.urlsafe_b64encode(_json.dumps(
            {"sub": "mallory", "rol": "admin", "exp": 9999999999}).encode()
        ).rstrip(b"=").decode("ascii")
        falsificado = f"{header}.{payload_admin}.{firma}"
        with self.assertRaises(ErrorAcceso):
            verificar_token(falsificado)

    def test_formato_invalido_se_rechaza(self):
        with self.assertRaises(ErrorAcceso):
            verificar_token("esto-no-es-un-token")

    def test_sin_secreto_configurado_no_genera_ni_verifica(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ErrorAcceso):
                generar_token("brighit", "admin")


class TestExigirRol(unittest.TestCase):
    def test_rol_permitido_no_lanza(self):
        exigir_rol({"usuario": "brighit", "rol": "admin"}, "admin")  # no debe lanzar

    def test_rol_no_permitido_lanza(self):
        with self.assertRaises(ErrorAcceso):
            exigir_rol({"usuario": "ana", "rol": "usuario"}, "admin")


class TestGestorUsuarios(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        os.unlink(self._tmp.name)  # el gestor lo crea él mismo al guardar
        self.gestor = GestorUsuarios(self._tmp.name)

    def tearDown(self):
        if os.path.exists(self._tmp.name):
            os.unlink(self._tmp.name)

    def test_agregar_y_autenticar_correctamente(self):
        self.gestor.agregar("brighit", "clave-fuerte-1", "admin")
        rol = self.gestor.autenticar("brighit", "clave-fuerte-1")
        self.assertEqual(rol, "admin")

    def test_autenticar_con_password_incorrecta_falla(self):
        self.gestor.agregar("brighit", "clave-fuerte-1", "admin")
        with self.assertRaises(ErrorAcceso):
            self.gestor.autenticar("brighit", "clave-incorrecta")

    def test_autenticar_usuario_inexistente_falla(self):
        with self.assertRaises(ErrorAcceso):
            self.gestor.autenticar("nadie", "cualquier-cosa")

    def test_rol_invalido_se_rechaza(self):
        with self.assertRaises(ErrorAcceso):
            self.gestor.agregar("brighit", "clave-fuerte-1", "superadmin")

    def test_la_contrasena_nunca_se_guarda_en_texto_plano(self):
        self.gestor.agregar("brighit", "clave-muy-identificable-xyz", "usuario")
        with open(self._tmp.name, "r", encoding="utf-8") as f:
            contenido = f.read()
        self.assertNotIn("clave-muy-identificable-xyz", contenido)
        datos = json.loads(contenido)
        self.assertIn("hash", datos["brighit"])
        self.assertIn("sal", datos["brighit"])

    def test_listar_no_expone_hashes_ni_sales(self):
        self.gestor.agregar("brighit", "clave-fuerte-1", "admin")
        self.gestor.agregar("ana", "clave-fuerte-2", "usuario")
        listado = self.gestor.listar()
        self.assertEqual(
            sorted(listado, key=lambda u: u["usuario"]),
            [{"usuario": "ana", "rol": "usuario"}, {"usuario": "brighit", "rol": "admin"}],
        )
        for u in listado:
            self.assertNotIn("hash", u)
            self.assertNotIn("sal", u)

    def test_eliminar_usuario_existente_y_luego_no_autentica(self):
        self.gestor.agregar("ana", "clave-fuerte-2", "usuario")
        self.gestor.eliminar("ana")
        self.assertFalse(self.gestor.existe("ana"))
        with self.assertRaises(ErrorAcceso):
            self.gestor.autenticar("ana", "clave-fuerte-2")

    def test_eliminar_usuario_inexistente_falla(self):
        with self.assertRaises(ErrorAcceso):
            self.gestor.eliminar("fantasma")

    def test_agregar_actualiza_password_si_el_usuario_ya_existe(self):
        self.gestor.agregar("ana", "clave-vieja", "usuario")
        self.gestor.agregar("ana", "clave-nueva", "usuario")
        with self.assertRaises(ErrorAcceso):
            self.gestor.autenticar("ana", "clave-vieja")
        self.assertEqual(self.gestor.autenticar("ana", "clave-nueva"), "usuario")


if __name__ == "__main__":
    unittest.main()
