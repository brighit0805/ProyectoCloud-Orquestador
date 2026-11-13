"""Pruebas de R1B: composición de topologías, servicio multiusuario, plantillas,
imágenes, consola, consumo de recursos y CLI. Todo con el driver Linux SIMULADO."""

import contextlib
import copy
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from unittest.mock import patch

from slice_orchestrator import cli, composicion, vm_placement
from slice_orchestrator.models import EstadoSlice, TipoTopologia
from slice_orchestrator.servicio_slices import (
    ErrorServicio,
    NoEncontrado,
    ServicioSlices,
    TTL_TOKEN_CONSOLA_S,
)
from slice_orchestrator.topology_manager import construir_topologia


def spec_ex1(nombre="demo-ex1"):
    return {
        "nombre": nombre,
        "topologia": {"base": {"tipo": "anillo", "nodos": 4},
                      "injertos": [{"sobre": "vm4", "tipo": "lineal", "nodos": 2}]},
        "flavores": {"vm1": "ubuntu", "vm2": "cirros", "vm3": "ubuntu",
                     "vm4": "ubuntu", "vm5": "ubuntu", "vm6": "cirros"},
        "acceso_internet": ["vm1", "vm3"],
        "zona_disponibilidad": "az-lab",
    }


def spec_simple(nombre, tipo="anillo", nodos=3, **extra):
    return {"nombre": nombre, "topologia": {"base": {"tipo": tipo, "nodos": nodos}}, **extra}


def pares(detalle):
    return {frozenset((e["a"], e["b"])) for e in detalle["enlaces"]}


class RelojFalso:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------------------
class TestComposicion(unittest.TestCase):
    def test_malla_y_arbol_generan_los_enlaces_del_catalogo(self):
        malla = construir_topologia("s", "m", "u", TipoTopologia.MALLA, 5)
        self.assertEqual(len(malla.enlaces), 10)                     # n(n-1)/2
        arbol = construir_topologia("s", "a", "u", TipoTopologia.ARBOL, 7, ramificacion=2)
        self.assertEqual(len(arbol.enlaces), 6)                      # n-1
        self.assertEqual(composicion.grado(arbol, arbol.nodos[0].id), 2)
        self.assertTrue(composicion.es_conexo(arbol))

    def test_restricciones_del_catalogo(self):
        for tipo, n in ((TipoTopologia.MALLA, 2), (TipoTopologia.ARBOL, 2), (TipoTopologia.ANILLO, 2)):
            with self.assertRaises(ValueError):
                construir_topologia("s", "x", "u", tipo, n)
        with self.assertRaises(ValueError):
            construir_topologia("s", "x", "u", TipoTopologia.ARBOL, 5, ramificacion=1)

    def test_clasificar_reconoce_las_figuras_del_catalogo(self):
        for tipo, n in ((TipoTopologia.LINEAL, 4), (TipoTopologia.ANILLO, 5),
                        (TipoTopologia.MALLA, 5), (TipoTopologia.ARBOL, 7)):
            self.assertEqual(composicion.clasificar(construir_topologia("s", "x", "u", tipo, n)), tipo)

    def test_ex1_se_compone_con_injertar_y_queda_arbitraria(self):
        slc = construir_topologia("s", "demo", "u", TipoTopologia.ANILLO, 4)
        nuevos = composicion.injertar(slc, "vm4", TipoTopologia.LINEAL, 2)
        self.assertEqual([composicion.clave_de(n) for n in nuevos], ["vm5", "vm6"])
        self.assertEqual((len(slc.nodos), len(slc.enlaces)), (6, 6))
        self.assertEqual(slc.tipo_topologia, TipoTopologia.ARBITRARIA)
        self.assertEqual(composicion.validar(slc), [])

    def test_agregar_y_quitar_enlace(self):
        slc = construir_topologia("s", "demo", "u", TipoTopologia.LINEAL, 4)
        composicion.agregar_enlace(slc, "vm1", "vm4")
        self.assertEqual(composicion.clasificar(slc), TipoTopologia.ANILLO)
        with self.assertRaises(ValueError):
            composicion.agregar_enlace(slc, "vm1", "vm4")            # duplicado
        with self.assertRaises(ValueError):
            composicion.agregar_enlace(slc, "vm2", "vm2")            # consigo mismo
        with self.assertRaises(ValueError):
            composicion.agregar_enlace(slc, "vm2", "vm9")            # no existe
        composicion.quitar_enlace(slc, "vm4", "vm1")                 # el orden no importa
        self.assertEqual(composicion.clasificar(slc), TipoTopologia.LINEAL)
        with self.assertRaises(ValueError):
            composicion.quitar_enlace(slc, "vm1", "vm3")             # no existía

    def test_eliminar_nodo_quita_sus_enlaces_y_las_claves_no_cambian(self):
        slc = construir_topologia("s", "demo", "u", TipoTopologia.ANILLO, 5)
        composicion.eliminar_nodo(slc, "vm2")
        self.assertEqual([composicion.clave_de(n) for n in slc.nodos], ["vm1", "vm3", "vm4", "vm5"])
        self.assertEqual(len(slc.enlaces), 3)
        nuevo = composicion.agregar_nodo(slc)
        self.assertEqual(composicion.clave_de(nuevo), "vm6")         # no reutiliza vm2
        self.assertEqual(len({n.id for n in slc.nodos}), 5)

    def test_validar_detecta_grafo_desconectado_y_exceso_de_interfaces(self):
        slc = construir_topologia("s", "demo", "u", TipoTopologia.LINEAL, 3)
        composicion.agregar_nodo(slc)
        self.assertTrue(any("conexo" in p for p in composicion.validar(slc)))
        malla = construir_topologia("s", "m", "u", TipoTopologia.MALLA, composicion.MAX_INTERFACES_POR_NODO + 2)
        self.assertTrue(any("interfaces" in p for p in composicion.validar(malla)))

    def test_injertar_invalido_no_deja_el_slice_a_medias(self):
        slc = construir_topologia("s", "demo", "u", TipoTopologia.LINEAL, 4)
        antes = copy.deepcopy(slc)
        with self.assertRaises(ValueError):
            composicion.injertar(slc, "vm1", TipoTopologia.ANILLO, 1)    # anillo con 2 nodos
        self.assertEqual(slc, antes)

    def test_capacidad_por_vm_y_rangos(self):
        slc = construir_topologia("s", "demo", "u", TipoTopologia.LINEAL, 2)
        n = slc.nodos[0]
        composicion.aplicar_capacidad(n, flavor="cirros", vcpu=2, ram_mb=1024, disco_gb=10)
        self.assertEqual((n.vcpu, n.ram_mb, n.disco_gb, n.flavor), (2, 1024, 10, "personalizado"))
        self.assertEqual(n.imagen, "cirros-0.6.2.qcow2")
        for malo in ({"vcpu": 0}, {"vcpu": 99}, {"ram_mb": 10}, {"disco_gb": -1}, {"flavor": "xl"}):
            with self.assertRaises(ValueError):
                composicion.aplicar_capacidad(n, **malo)
        self.assertEqual((n.vcpu, n.ram_mb), (2, 1024))                  # los rechazos no modifican


# ---------------------------------------------------------------------------
class TestServicio(unittest.TestCase):
    def setUp(self):
        self.svc = ServicioSlices()

    def test_crear_ex1_en_las_dos_formas_del_hld_da_el_mismo_grafo(self):
        a = self.svc.crear_slice("ana", spec_ex1("a-ex1"))
        forma_37 = spec_ex1("b-ex1")
        forma_37["topologia"] = {"base": "anillo", "nodos": 4,
                                 "ramas": [{"desde": "vm4", "tipo": "lineal", "nodos": 2}]}
        b = self.svc.crear_slice("ana", forma_37)
        self.assertEqual(pares(a), pares(b))
        self.assertEqual(a["estado"], "borrador")
        self.assertEqual(len(a["nodos"]), 6)

    def test_ex1_desplegada_reparte_por_round_robin_y_solo_vm1_vm3_con_internet(self):
        d = self.svc.crear_slice("ana", spec_ex1(), desplegar=True, estrategia="round_robin")
        self.assertEqual(d["estado"], "activo")
        hosts = {n["clave"]: n["host"] for n in d["nodos"]}
        self.assertEqual(hosts, {"vm1": "worker1", "vm4": "worker1", "vm2": "worker2",
                                 "vm5": "worker2", "vm3": "worker3", "vm6": "worker3"})
        self.assertEqual({n["clave"] for n in d["nodos"] if n["acceso_internet"]}, {"vm1", "vm3"})
        self.assertEqual(len({e["vlan"] for e in d["enlaces"]}), 6)

    def test_el_sistema_usa_el_scheduler_por_defecto_sin_que_el_usuario_elija(self):
        """Hallazgo del 12-nov: el Scheduler (filtros+pesadores+función
        objetivo, Lab 7) ya existía pero estaba inalcanzable desde el flujo
        real de usuario -- solo se probaba con un script aparte.

        Hallazgo del mismo día, en una segunda vuelta (confirmado con
        German por WhatsApp: "su solución debería decidirlo"): la elección
        de ESTRATEGIA no debe ser del usuario, sino del sistema. Por eso
        ServicioSlices ya no recibe --estrategia desde la CLI y usa
        'objetivo' (el Scheduler) por defecto siempre que no se indique lo
        contrario explícitamente (los otros valores quedan solo para uso
        interno/pruebas). Esta prueba confirma que, sin pasar ninguna
        estrategia, el camino por defecto ya pasa por el Scheduler."""
        d = self.svc.crear_slice("ana", spec_ex1("obj"), desplegar=True)
        self.assertEqual(d["estado"], "activo")
        resultado = vm_placement.obtener_ultimo_resultado()
        self.assertIsNotNone(resultado)       # confirma que SI paso por el Scheduler
        self.assertTrue(all(n["host"] for n in d["nodos"]))

    def test_editar_zona_permite_desplegar_despues(self):
        """Quien olvidó --zona al crear el slice no queda bloqueado: puede
        fijarla luego con 'editar --zona' antes de desplegar."""
        d = self.svc.crear_slice("ana", spec_simple("tardio", "anillo", 3))
        d = self.svc.editar_slice("ana", d["id"], [{"op": "zona", "zona": "az-lab"}])
        self.assertEqual(d["zona"], "az-lab")
        d = self.svc.desplegar_slice("ana", d["id"])
        self.assertEqual(d["estado"], "activo")

    def test_editar_zona_invalida_se_rechaza(self):
        d = self.svc.crear_slice("ana", spec_simple("x", "anillo", 3))
        with self.assertRaises(ErrorServicio):
            self.svc.editar_slice("ana", d["id"], [{"op": "zona", "zona": "marte"}])

    def test_desplegar_sin_zona_sigue_funcionando_con_el_driver_simulado(self):
        """No regresión, y ajuste tras ocultar --estrategia: con el driver
        Linux SIMULADO (el de uso normal/pruebas), desplegar sin zona sigue
        funcionando igual que antes -- aunque por dentro siempre se use
        'objetivo', la zona solo se vuelve obligatoria contra el driver
        OpenStack REAL (ver test_driver_real_sin_zona_se_rechaza)."""
        for estrategia in (None, "balanceado", "round_robin", "objetivo"):
            nombre = "ok-" + (estrategia or "default").replace("_", "-")
            kwargs = {} if estrategia is None else {"estrategia": estrategia}
            d = self.svc.crear_slice("ana", spec_simple(nombre, "anillo", 3),
                                     desplegar=True, **kwargs)
            self.assertEqual(d["estado"], "activo")

    def test_driver_real_sin_zona_se_rechaza(self):
        """Con el driver OpenStack real, la zona sí es obligatoria -- es la
        única condición que la dispara ahora que la estrategia ya no la
        elige el usuario (ver servicio_slices.desplegar_slice)."""
        from slice_orchestrator.driver_openstack import ModoOpenStack

        class _DriverRealFalso:
            modo = ModoOpenStack.REAL

        svc = ServicioSlices(driver=_DriverRealFalso())
        d = svc.crear_slice("ana", spec_simple("real-sin-zona", "anillo", 3))
        with self.assertRaises(ErrorServicio) as ctx:
            svc.desplegar_slice("ana", d["id"])
        self.assertIn("zona de disponibilidad es obligatoria", str(ctx.exception))
        self.assertEqual(svc.ver_slice("ana", d["id"])["estado"], "borrador")  # no quedó en error

    def test_limpiar_residuos_no_soportado_por_el_driver_simulado(self):
        """El driver Linux simulado no tiene limpiar_residuos() (solo tiene
        sentido contra un clúster OpenStack real, que puede acumular residuos
        entre sesiones -- ver hallazgo del 12-nov en estado-proyecto): el
        servicio debe rechazarlo con un mensaje claro, no reventar con
        AttributeError."""
        with self.assertRaises(ErrorServicio) as ctx:
            self.svc.limpiar_residuos()
        self.assertIn("OpenStack real", str(ctx.exception))

    def test_limpiar_residuos_delega_en_el_driver(self):
        """Duck-typing, igual que preparar_slice/descubrir_hosts (ver
        _DriverRealFalso arriba): si el driver activo sí ofrece
        limpiar_residuos(), el servicio solo delega y devuelve exactamente
        lo que el driver reporta, pasando 'confirmar' tal cual (y los
        slice_id que tiene registrados, ver test siguiente)."""
        llamadas = []

        class _DriverConLimpieza:
            def limpiar_residuos(self, confirmar=False, ids_protegidos=None):
                llamadas.append(confirmar)
                return {"proyectos": [{"id": "p1", "name": "orq_viejo"}], "redes": [], "flavors": []}

        svc = ServicioSlices(driver=_DriverConLimpieza())
        r = svc.limpiar_residuos()
        self.assertEqual(llamadas, [False])
        self.assertEqual(r["proyectos"][0]["name"], "orq_viejo")
        svc.limpiar_residuos(confirmar=True)
        self.assertEqual(llamadas, [False, True])

    def test_limpiar_residuos_nunca_incluye_un_slice_registrado(self):
        """Hallazgo 13-nov-2026 (ver estado-proyecto §9novies): se borro por
        error el proyecto real de un slice ACTIVO porque el driver creyo,
        solo por lo que reporto Nova, que no tenia instancias. El servicio
        ahora SIEMPRE pasa al driver los slice_id que el sigue teniendo
        registrados localmente, para que ese slice nunca se trate como
        huerfano sin importar lo que diga Nova."""
        capturado = {}

        class _DriverConLimpieza:
            def limpiar_residuos(self, confirmar=False, ids_protegidos=None):
                capturado["ids_protegidos"] = ids_protegidos
                return {"proyectos": [], "redes": [], "flavors": []}

        svc = ServicioSlices(driver=_DriverConLimpieza())
        d = svc.crear_slice("ana", spec_simple("protegido", "lineal", 2))
        svc.limpiar_residuos(confirmar=True)
        self.assertIn(d["id"], capturado["ids_protegidos"])

    def test_topologias_predefinidas_malla_arbol_y_bus(self):
        m = self.svc.crear_slice("ana", spec_simple("m", "malla", 4), desplegar=True)
        self.assertEqual((m["tipo"], len(m["enlaces"])), ("malla", 6))
        a = self.svc.crear_slice("ana", {"nombre": "a", "topologia": {
            "base": {"tipo": "arbol", "nodos": 7, "ramificacion": 3}}})
        self.assertEqual((a["tipo"], len(a["enlaces"])), ("arbol", 6))
        # Bus (13-nov): cadena de n-1 enlaces, todos compartiendo la MISMA
        # VLAN/red -- un único dominio de difusión, no n-1 enlaces aislados.
        b = self.svc.crear_slice("ana", spec_simple("b", "bus", 3), desplegar=True)
        self.assertEqual((b["tipo"], len(b["enlaces"])), ("bus", 2))
        vlans = {e["vlan"] for e in b["enlaces"]}
        redes = {e["red"] for e in b["enlaces"]}
        self.assertEqual((len(vlans), len(redes)), (1, 1))
        self.assertTrue(next(t for t in self.svc.listar_topologias() if t["tipo"] == "bus")["disponible"])

    def test_bus_no_se_soporta_todavia_como_injerto(self):
        # Decisión de alcance (13-nov): bus solo como topología base por
        # ahora -- como injerto el slice queda MIXTA y nunca recibiría la
        # VLAN única (ver _TIPOS_INJERTO_DISPONIBLES), así que se rechaza
        # explícitamente en vez de simular un bus que no es real.
        with self.assertRaises(ErrorServicio) as ctx:
            self.svc.crear_slice("ana", {"nombre": "mx", "topologia": {
                "base": {"tipo": "anillo", "nodos": 3},
                "injertos": [{"sobre": "vm1", "tipo": "bus", "nodos": 1}]}})
        self.assertIn("injerto", str(ctx.exception))
        d = self.svc.crear_slice("ana", spec_simple("e2", "lineal", 3))
        with self.assertRaises(ErrorServicio) as ctx:
            self.svc.editar_slice("ana", d["id"], [
                {"op": "injertar", "tipo": "bus", "sobre": "vm1", "nodos": 1}])
        self.assertIn("injerto", str(ctx.exception))

    def test_capacidad_de_cada_vm_se_respeta_y_se_valida(self):
        spec = spec_simple("cap", "lineal", 2, capacidad={"vm2": {"vcpu": 4, "ram_mb": 2048, "disco_gb": 20}})
        d = self.svc.crear_slice("ana", spec)
        vm2 = next(n for n in d["nodos"] if n["clave"] == "vm2")
        self.assertEqual((vm2["vcpu"], vm2["ram_mb"], vm2["disco_gb"]), (4, 2048, 20))
        for capacidad in ({"vm1": {"vcpu": 500}}, {"vm9": {"vcpu": 1}}, {"vm1": {"gpu": 1}},
                          {"vm1": {"vcpu": True}}, {"vm1": "mucho"}):
            with self.assertRaises(ErrorServicio):
                self.svc.crear_slice("ana", spec_simple("x", "lineal", 2, capacidad=capacidad))

    def test_entradas_invalidas_se_rechazan_sin_dejar_basura(self):
        malas = [spec_simple("Mal Nombre"), spec_simple("a" * 31), spec_simple("x", "anillo", 2),
                 spec_simple("x", "estrella", 4), spec_simple("x", "anillo", "4"),
                 spec_simple("x", "anillo", 100), spec_simple("x", "anillo", 3, zona_disponibilidad="marte"),
                 {"nombre": "x"}, {"nombre": "x", "topologia": "anillo"}, "texto"]
        for spec in malas:
            with self.assertRaises(ErrorServicio, msg=str(spec)[:60]):
                self.svc.crear_slice("ana", spec)
        self.assertEqual(self.svc.listar_slices("ana"), [])
        self.assertEqual(self.svc.crear_slice("ana", spec_simple("ok"))["id"], "slice-001")  # id no se quemó

    def test_nombre_duplicado_por_usuario_pero_no_entre_usuarios(self):
        self.svc.crear_slice("ana", spec_simple("lab"))
        with self.assertRaises(ErrorServicio):
            self.svc.crear_slice("ana", spec_simple("lab"))
        self.svc.crear_slice("bob", spec_simple("lab"))

    def test_usuario_invalido(self):
        for u in ("", "a b", "../x", None, "x" * 65):
            with self.assertRaises(ErrorServicio):
                self.svc.listar_slices(u)

    # ---- aislamiento entre usuarios (HLD §3.3) ------------------------------
    def test_un_usuario_no_ve_ni_toca_los_slices_de_otro(self):
        d = self.svc.crear_slice("ana", spec_simple("privado"), desplegar=True)
        sid = d["id"]
        self.assertEqual(self.svc.listar_slices("bob"), [])
        acciones = [
            lambda: self.svc.ver_slice("bob", sid),
            lambda: self.svc.eliminar_slice("bob", sid),
            lambda: self.svc.desplegar_slice("bob", sid),
            lambda: self.svc.editar_slice("bob", sid, [{"op": "agregar_nodo"}]),
            lambda: self.svc.exportar_plantilla("bob", sid),
            lambda: self.svc.emitir_token_consola("bob", sid, "vm1"),
        ]
        for accion in acciones:
            with self.assertRaises(NoEncontrado):
                accion()
        with self.assertRaises(NoEncontrado):               # mismo error que uno inexistente
            self.svc.ver_slice("bob", "slice-999")
        self.assertEqual(self.svc.ver_slice("ana", sid)["estado"], "activo")   # sigue intacto

    # ---- editar, listar, eliminar ----------------------------------------------
    def test_editar_borrador_compone_el_grafo(self):
        d = self.svc.crear_slice("ana", spec_simple("e", "anillo", 4))
        d = self.svc.editar_slice("ana", d["id"], [
            {"op": "injertar", "sobre": "vm4", "tipo": "lineal", "nodos": 2},
            {"op": "agregar_enlace", "a": "vm2", "b": "vm5"},
            {"op": "agregar_nodo", "capacidad": {"vcpu": 2}},
            {"op": "agregar_enlace", "a": "vm7", "b": "vm1"},
            {"op": "capacidad", "nodo": "vm3", "flavor": "cirros"},
            {"op": "acceso_internet", "nodo": "vm1", "valor": True},
        ])
        self.assertEqual((len(d["nodos"]), len(d["enlaces"]), d["tipo"]), (7, 8, "arbitraria"))
        self.assertTrue(next(n for n in d["nodos"] if n["clave"] == "vm1")["acceso_internet"])
        d = self.svc.desplegar_slice("ana", d["id"])
        self.assertEqual(d["estado"], "activo")

    def test_edicion_es_todo_o_nada(self):
        d = self.svc.crear_slice("ana", spec_simple("e", "lineal", 3))
        antes = self.svc.ver_slice("ana", d["id"])
        with self.assertRaises(ErrorServicio):
            self.svc.editar_slice("ana", d["id"], [{"op": "agregar_nodo"},
                                                   {"op": "agregar_enlace", "a": "vm1", "b": "vm99"}])
        self.assertEqual(self.svc.ver_slice("ana", d["id"]), antes)
        for op in ({"op": "volar"}, {"x": 1}, "texto", {"op": "injertar", "tipo": "estrella", "sobre": "vm1", "nodos": 1}):
            with self.assertRaises(ErrorServicio):
                self.svc.editar_slice("ana", d["id"], [op])
        with self.assertRaises(ErrorServicio):
            self.svc.editar_slice("ana", d["id"], [])

    def test_editar_slice_activo_con_grafo_desconectado_se_rechaza(self):
        # Desde el 13-nov un slice ACTIVO SÍ se puede editar (en vivo), pero
        # igual que en BORRADOR, el resultado debe seguir siendo un diseño
        # válido (conexo) -- agregar un nodo suelto sin conectarlo se rechaza.
        d = self.svc.crear_slice("ana", spec_simple("e"), desplegar=True)
        with self.assertRaises(ErrorServicio) as ctx:
            self.svc.editar_slice("ana", d["id"], [{"op": "agregar_nodo"}])
        self.assertIn("no es válido", str(ctx.exception))
        self.assertEqual(self.svc.ver_slice("ana", d["id"])["estado"], "activo")

    def test_editar_slice_activo_agrega_nodo_en_vivo(self):
        d = self.svc.crear_slice("ana", spec_simple("e"), desplegar=True)
        claves_viejas = {n["clave"] for n in d["nodos"]}
        nueva_clave = f"vm{len(d['nodos']) + 1}"      # próxima clave que asignará agregar_nodo
        resultado = self.svc.editar_slice("ana", d["id"], [
            {"op": "agregar_nodo"},
            {"op": "agregar_enlace", "a": d["nodos"][0]["clave"], "b": nueva_clave},
        ])
        self.assertEqual(resultado["estado"], "activo")
        self.assertEqual(len(resultado["nodos"]), len(d["nodos"]) + 1)
        nuevo = next(n for n in resultado["nodos"] if n["clave"] not in claves_viejas)
        self.assertEqual(nuevo["clave"], nueva_clave)
        self.assertIsNotNone(nuevo["host"])

    def test_no_se_despliega_un_diseno_desconectado(self):
        d = self.svc.crear_slice("ana", spec_simple("e", "lineal", 3))
        d = self.svc.editar_slice("ana", d["id"], [{"op": "agregar_nodo"}])
        with self.assertRaises(ErrorServicio) as ctx:
            self.svc.desplegar_slice("ana", d["id"])
        self.assertIn("conexo", str(ctx.exception))

    def test_listar_y_eliminar_liberan_recursos_y_vlan(self):
        d = self.svc.crear_slice("ana", spec_simple("uno", "anillo", 3), desplegar=True)
        vlans = {e["vlan"] for e in d["enlaces"]}
        usados = lambda: sum(h["vcpu_usados"] for h in self.svc.consumo_recursos("ana")["hosts"])
        self.assertEqual(usados(), 3)
        self.assertEqual([s["nombre"] for s in self.svc.listar_slices("ana")], ["uno"])
        self.assertEqual(self.svc.eliminar_slice("ana", d["id"])["estado"], "eliminado")
        self.assertEqual(self.svc.listar_slices("ana"), [])
        self.assertEqual(usados(), 0)
        otro = self.svc.crear_slice("ana", spec_simple("dos", "anillo", 3), desplegar=True)
        self.assertEqual({e["vlan"] for e in otro["enlaces"]}, vlans)      # las VLAN se reutilizan
        with self.assertRaises(NoEncontrado):
            self.svc.eliminar_slice("ana", d["id"])                         # ya no existe

    def test_slice_sin_capacidad_queda_en_error_y_no_consume_recursos(self):
        spec = spec_simple("grande", "lineal", 7, capacidad={f"vm{i}": {"vcpu": 8} for i in range(1, 8)})
        d = self.svc.crear_slice("ana", spec)                               # 7 × 8 = 56 > 48 vCPU
        with self.assertRaises(ErrorServicio) as ctx:
            self.svc.desplegar_slice("ana", d["id"])
        self.assertIn("capacidad", str(ctx.exception))
        self.assertEqual(self.svc.ver_slice("ana", d["id"])["estado"], "error")
        self.assertEqual(sum(h["vcpu_usados"] for h in self.svc.consumo_recursos("ana")["hosts"]), 0)
        d = self.svc.editar_slice("ana", d["id"], [{"op": "eliminar_nodo", "nodo": "vm7"},
                                                   {"op": "eliminar_nodo", "nodo": "vm6"},
                                                   {"op": "eliminar_nodo", "nodo": "vm5"}])
        self.assertEqual(self.svc.desplegar_slice("ana", d["id"])["estado"], "activo")   # reintento tras corregir

    # ---- consumo de recursos --------------------------------------------------
    def test_consumo_por_host_y_por_usuario(self):
        self.svc.crear_slice("ana", spec_simple("a", "anillo", 3, capacidad={"vm1": {"vcpu": 2, "ram_mb": 1024}}),
                             desplegar=True, estrategia="round_robin")
        self.svc.crear_slice("bob", spec_simple("b", "lineal", 2), desplegar=True)
        ana = self.svc.consumo_recursos("ana")
        self.assertEqual(ana["total_mio"]["vcpu"], 4)
        self.assertEqual([s["nombre"] for s in ana["mis_slices"]], ["a"])
        self.assertEqual(sum(h["vcpu_usados"] for h in ana["hosts"]), 6)     # el host muestra a todos
        self.assertEqual(self.svc.consumo_recursos("bob")["total_mio"]["vcpu"], 2)
        self.assertEqual({"host", "zona", "vcpu_usados", "vcpu_total", "ram_usada_mb",
                          "ram_total_mb", "disco_usado_gb"}, set(ana["hosts"][0]))


# ---------------------------------------------------------------------------
class TestPlantillas(unittest.TestCase):
    def setUp(self):
        self.svc = ServicioSlices()

    def test_exportar_e_importar_reproduce_el_diseno_sin_recursos(self):
        orig = self.svc.crear_slice("ana", spec_ex1(), desplegar=True)
        plantilla = self.svc.exportar_plantilla("ana", orig["id"])
        texto = json.dumps(plantilla)
        for ajeno in ("vlan", "host", "worker", "10.30.", "owner", "ana"):
            self.assertNotIn(ajeno, texto.replace("nombre_sugerido", "").replace("demo-ex1", ""))
        copia = self.svc.crear_desde_plantilla("bob", json.loads(texto), "mi-copia")
        self.assertEqual(copia["estado"], "borrador")
        self.assertEqual(copia["owner"], "bob")
        self.assertEqual(pares(copia), pares(orig))
        proyeccion = lambda d: [(n["clave"], n["flavor"], n["vcpu"], n["ram_mb"], n["disco_gb"],
                                 n["imagen"], n["acceso_internet"]) for n in d["nodos"]]
        self.assertEqual(proyeccion(copia), proyeccion(orig))
        self.assertTrue(all(n["host"] is None for n in copia["nodos"]))
        self.assertEqual(self.svc.desplegar_slice("bob", copia["id"])["estado"], "activo")  # ambos coexisten
        self.assertEqual(self.svc.ver_slice("ana", orig["id"])["estado"], "activo")

    def test_nombre_sugerido_y_conflictos(self):
        d = self.svc.crear_slice("ana", spec_simple("base"))
        p = self.svc.exportar_plantilla("ana", d["id"])
        with self.assertRaises(ErrorServicio):
            self.svc.crear_desde_plantilla("ana", p)                          # mismo nombre sugerido
        self.assertEqual(self.svc.crear_desde_plantilla("bob", p)["nombre"], "base")

    def test_plantillas_hostiles_o_mal_formadas_se_rechazan(self):
        d = self.svc.crear_slice("ana", spec_simple("base", "lineal", 3))
        buena = self.svc.exportar_plantilla("ana", d["id"])

        def con(**cambios):
            p = copy.deepcopy(buena)
            p.update(cambios)
            return p

        def nodo(i, **c):
            p = copy.deepcopy(buena)
            p["nodos"][i].update(c)
            return p

        malas = {
            "no es objeto": "texto", "sin version": {k: v for k, v in buena.items() if k != "version"},
            "version futura": con(version="9.9"), "sin nodos": con(nodos=[]),
            "demasiados nodos": con(nodos=[{**buena["nodos"][0], "clave": f"vm{i}"} for i in range(1, 80)],
                                    enlaces=[]),
            "clave con ruta": nodo(0, clave="../etc"), "clave repetida": nodo(1, clave="vm1"),
            "imagen ajena": nodo(0, imagen="/etc/passwd"), "imagen que no existe": nodo(0, imagen="otra.qcow2"),
            "vcpu enorme": nodo(0, vcpu=1000), "vcpu booleano": nodo(0, vcpu=True),
            "ram texto": nodo(0, ram_mb="mucha"), "internet no booleano": nodo(0, acceso_internet="sí"),
            "flavor desconocido": nodo(0, flavor="xl"),
            "enlace a nodo inexistente": con(enlaces=[["vm1", "vm9"]]),
            "enlace mal formado": con(enlaces=[["vm1"]]), "enlace consigo mismo": con(enlaces=[["vm1", "vm1"]]),
            "enlace repetido": con(enlaces=[["vm1", "vm2"], ["vm2", "vm1"]]),
            "grafo desconectado": con(enlaces=[["vm1", "vm2"]]),
        }
        for motivo, plantilla in malas.items():
            with self.assertRaises(ErrorServicio, msg=motivo):
                self.svc.crear_desde_plantilla("bob", plantilla, "importada")
        self.assertEqual(self.svc.listar_slices("bob"), [])


# ---------------------------------------------------------------------------
class TestImagenes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = ServicioSlices()

    def archivo(self, nombre="x.qcow2", contenido=b"QFI\xfb" + b"\0" * 100):
        ruta = os.path.join(self.tmp.name, nombre)
        with open(ruta, "wb") as f:
            f.write(contenido)
        return ruta

    def test_el_catalogo_inicial_trae_las_imagenes_de_los_flavors(self):
        self.assertEqual({i["nombre"] for i in self.svc.listar_imagenes()},
                         {"ubuntu-22.04.qcow2", "cirros-0.6.2.qcow2"})

    def test_importar_registra_tamano_y_hash_y_se_puede_usar(self):
        i = self.svc.importar_imagen("ana", "debian-12.qcow2", self.archivo())
        self.assertEqual((i["tamano_bytes"], len(i["sha256"]), i["importada_por"]), (104, 64, "ana"))
        d = self.svc.crear_slice("ana", spec_simple("d", "lineal", 2,
                                                    capacidad={"vm1": {"imagen": "debian-12.qcow2"}}))
        self.assertEqual(next(n for n in d["nodos"] if n["clave"] == "vm1")["imagen"], "debian-12.qcow2")
        self.assertEqual(self.svc.desplegar_slice("ana", d["id"])["estado"], "activo")

    def test_imagen_no_registrada_se_rechaza_al_crear(self):
        with self.assertRaises(ErrorServicio) as ctx:
            self.svc.crear_slice("ana", spec_simple("d", "lineal", 2, capacidad={"vm1": {"imagen": "nueva.qcow2"}}))
        self.assertIn("importar", str(ctx.exception))

    def test_importacion_invalida(self):
        casos = [("sin-extension", self.archivo("a.qcow2")), ("../mala.qcow2", self.archivo("b.qcow2")),
                 ("a b.qcow2", self.archivo("c.qcow2")), ("nofirma.qcow2", self.archivo("d.qcow2", b"MZ\x90\0")),
                 ("noexiste.qcow2", os.path.join(self.tmp.name, "no-esta")),
                 ("dir.qcow2", self.tmp.name), ("ubuntu-22.04.qcow2", self.archivo("e.qcow2"))]
        for nombre, origen in casos:
            with self.assertRaises(ErrorServicio, msg=nombre):
                self.svc.importar_imagen("ana", nombre, origen)
        self.assertEqual(len(self.svc.listar_imagenes()), 2)


# ---------------------------------------------------------------------------
class TestConsola(unittest.TestCase):
    def setUp(self):
        self.reloj = RelojFalso()
        self.svc = ServicioSlices(reloj=self.reloj)
        self.d = self.svc.crear_slice("ana", spec_simple("c", "anillo", 3), desplegar=True)

    def test_token_de_un_solo_uso_con_datos_de_la_vm(self):
        t = self.svc.emitir_token_consola("ana", self.d["id"], "vm2")
        self.assertEqual((t["expira_en"], t["simulado"]), (TTL_TOKEN_CONSOLA_S, True))
        self.assertEqual(t["host"], next(n["host"] for n in self.d["nodos"] if n["clave"] == "vm2"))
        canje = self.svc.canjear_token_consola(t["token"])
        self.assertEqual((canje["usuario"], canje["slice_id"], canje["nodo"]), ("ana", self.d["id"], "vm2"))
        with self.assertRaises(ErrorServicio):
            self.svc.canjear_token_consola(t["token"])                       # ya usado

    def test_el_token_caduca(self):
        t = self.svc.emitir_token_consola("ana", self.d["id"], "vm1")
        self.reloj.t += TTL_TOKEN_CONSOLA_S + 1
        with self.assertRaises(ErrorServicio):
            self.svc.canjear_token_consola(t["token"])

    def test_los_tokens_son_distintos_y_adivinarlos_no_sirve(self):
        a = self.svc.emitir_token_consola("ana", self.d["id"], "vm1")["token"]
        b = self.svc.emitir_token_consola("ana", self.d["id"], "vm1")["token"]
        self.assertNotEqual(a, b)
        for falso in ("", "x", a[:-1], a + "x", None):
            with self.assertRaises(ErrorServicio):
                self.svc.canjear_token_consola(falso)

    def test_solo_slices_activos_propios_y_nodos_existentes(self):
        with self.assertRaises(NoEncontrado):
            self.svc.emitir_token_consola("bob", self.d["id"], "vm1")
        with self.assertRaises(ErrorServicio):
            self.svc.emitir_token_consola("ana", self.d["id"], "vm9")
        borrador = self.svc.crear_slice("ana", spec_simple("b"))
        with self.assertRaises(ErrorServicio):
            self.svc.emitir_token_consola("ana", borrador["id"], "vm1")

    def test_eliminar_el_slice_invalida_sus_tokens(self):
        t = self.svc.emitir_token_consola("ana", self.d["id"], "vm1")
        self.svc.eliminar_slice("ana", self.d["id"])
        with self.assertRaises(ErrorServicio):
            self.svc.canjear_token_consola(t["token"])


# ---------------------------------------------------------------------------
class TestPersistenciaYConcurrencia(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ruta = os.path.join(self.tmp.name, "estado.json")

    def test_el_estado_sobrevive_a_un_reinicio_y_sigue_siendo_consistente(self):
        s1 = ServicioSlices(self.ruta)
        d = s1.crear_slice("ana", spec_ex1(), desplegar=True)
        t = s1.emitir_token_consola("ana", d["id"], "vm1")
        s2 = ServicioSlices(self.ruta)                                        # "otro proceso"
        self.assertEqual(s2.ver_slice("ana", d["id"]), d)
        self.assertEqual(s2.listar_slices("bob"), [])
        nuevo = s2.crear_slice("bob", spec_simple("b", "anillo", 3), desplegar=True)
        self.assertNotIn(nuevo["id"], (d["id"],))
        self.assertFalse({e["vlan"] for e in nuevo["enlaces"]} & {e["vlan"] for e in d["enlaces"]})
        self.assertEqual(s2.canjear_token_consola(t["token"])["nodo"], "vm1")  # el token persistió
        self.assertEqual(s2.eliminar_slice("ana", d["id"])["estado"], "eliminado")
        self.assertEqual(ServicioSlices(self.ruta).listar_slices("ana"), [])

    def test_el_token_no_se_guarda_en_claro(self):
        s = ServicioSlices(self.ruta)
        d = s.crear_slice("ana", spec_simple("c"), desplegar=True)
        token = s.emitir_token_consola("ana", d["id"], "vm1")["token"]
        with open(self.ruta, encoding="utf-8") as f:
            self.assertNotIn(token, f.read())

    def test_archivo_de_estado_corrupto_da_un_error_claro(self):
        with open(self.ruta, "w", encoding="utf-8") as f:
            f.write("{no es json")
        with self.assertRaises(ErrorServicio) as ctx:
            ServicioSlices(self.ruta)
        self.assertIn("estado", str(ctx.exception))

    def test_no_quedan_temporales_tras_guardar(self):
        s = ServicioSlices(self.ruta)
        s.crear_slice("ana", spec_simple("c"))
        self.assertEqual(os.listdir(self.tmp.name), ["estado.json"])

    def test_muchos_usuarios_a_la_vez_no_se_pisan(self):
        s = ServicioSlices(self.ruta)
        usuarios = [f"user{i:02d}" for i in range(12)]
        barrera = threading.Barrier(len(usuarios))

        def sesion(u):
            barrera.wait()                                                    # todos arrancan a la vez
            d = s.crear_slice(u, spec_simple("lab", "anillo", 3), desplegar=True)
            s.emitir_token_consola(u, d["id"], "vm1")
            return u, d

        with ThreadPoolExecutor(max_workers=len(usuarios)) as pool:
            resultados = list(pool.map(sesion, usuarios))

        ids = [d["id"] for _, d in resultados]
        vlans = [e["vlan"] for _, d in resultados for e in d["enlaces"]]
        self.assertEqual(len(set(ids)), 12)
        self.assertEqual(len(vlans), len(set(vlans)))                         # ninguna VLAN repetida
        for u, d in resultados:
            propios = s.listar_slices(u)
            self.assertEqual([x["id"] for x in propios], [d["id"]])           # cada uno ve solo lo suyo
            self.assertEqual(s.consumo_recursos(u)["total_mio"]["vcpu"], 3)
        self.assertEqual(sum(h["vcpu_usados"] for h in s.consumo_recursos("user00")["hosts"]), 36)
        self.assertEqual(len(ServicioSlices(self.ruta).listar_slices("user05")), 1)   # y quedó en disco


# ---------------------------------------------------------------------------
class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ruta = os.path.join(self.tmp.name, "estado.json")

    def correr(self, *args, usuario="ana"):
        salida, errores = io.StringIO(), io.StringIO()
        base = ["--estado", self.ruta] + (["-u", usuario] if usuario else [])
        with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(errores):
            codigo = cli.main(base + list(args))
        return codigo, salida.getvalue(), errores.getvalue()

    def test_flujo_completo_por_consola(self):
        c, out, _ = self.correr("topologias", usuario=None)
        self.assertEqual(c, 0)
        self.assertIn("malla", out)
        self.assertIn("bus", out)
        c, out, _ = self.correr("slice", "crear", "--nombre", "ex1", "--tipo", "anillo", "--nodos", "4",
                                "--rama", "vm4:lineal:2", "--flavor", "vm2=cirros", "--flavor", "vm6=cirros",
                                "--internet", "vm1", "--internet", "vm3", "--zona", "az-lab",
                                "--capacidad", "vm3=vcpu:2,ram:1024,disco:8", "--desplegar")
        self.assertEqual(c, 0)
        self.assertIn("activo", out)
        c, out, _ = self.correr("slice", "listar")
        self.assertIn("ex1", out)
        c, out, _ = self.correr("slice", "listar", usuario="bob")
        self.assertIn("No tiene slices", out)
        c, out, _ = self.correr("--json", "recursos")
        self.assertEqual(json.loads(out)["total_mio"]["vcpu"], 7)
        archivo = os.path.join(self.tmp.name, "plantilla.json")
        self.assertEqual(self.correr("slice", "exportar", "slice-001", archivo)[0], 0)
        c, out, _ = self.correr("slice", "importar", archivo, "--nombre", "copia", usuario="bob")
        self.assertEqual(c, 0)
        c, out, _ = self.correr("slice", "editar", "slice-002", "--agregar-nodos", "1",
                                "--agregar-enlace", "vm7,vm1", "--quitar-enlace", "vm1,vm2", usuario="bob")
        self.assertEqual(c, 0)
        self.assertIn("vm7", out)
        c, out, _ = self.correr("--json", "consola", "emitir", "slice-001", "vm1")
        token = json.loads(out)["token"]
        self.assertEqual(self.correr("consola", "canjear", token, usuario=None)[0], 0)
        self.assertEqual(self.correr("consola", "canjear", token, usuario=None)[0], 1)
        self.assertEqual(self.correr("slice", "eliminar", "slice-001")[0], 0)

    def test_errores_dan_codigo_1_y_mensaje_claro_sin_traza(self):
        c, _, err = self.correr("slice", "listar", usuario=None)
        self.assertEqual(c, 1)
        self.assertIn("usuario", err)
        c, _, err = self.correr("slice", "ver", "slice-123")
        self.assertEqual((c, "no existe" in err), (1, True))
        c, _, err = self.correr("slice", "crear", "--nombre", "x", "--tipo", "estrella", "--nodos", "3")
        self.assertEqual((c, "estrella" in err), (1, True))
        c, _, err = self.correr("slice", "crear", "--spec", os.path.join(self.tmp.name, "falta.json"))
        self.assertEqual(c, 1)
        c, _, err = self.correr("imagen", "importar", "x.qcow2", "/no/existe")
        self.assertEqual(c, 1)
        self.assertNotIn("Traceback", err)

    def test_sintaxis_invalida_la_rechaza_argparse(self):
        for args in (["slice", "crear", "--nombre", "x", "--tipo", "anillo", "--nodos", "3", "--rama", "mala"],
                     ["slice", "crear", "--nombre", "x", "--tipo", "anillo", "--nodos", "3",
                      "--capacidad", "vm1=vcpu"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.correr(*args)

    def test_la_cli_ya_no_deja_elegir_estrategia_y_usa_el_scheduler_siempre(self):
        """Hallazgo del 12-nov, en dos partes:
        1) Antes de la primera parte de este cambio, argparse rechazaba
           '--estrategia objetivo' en la CLI real, aunque el Scheduler ya
           funcionaba por dentro.
        2) Confirmado el mismo día con German por WhatsApp ("su solución
           debería decidirlo"): la elección de estrategia no debe ser del
           usuario. Por eso '--estrategia' se eliminó de la CLI por
           completo -- ya ni siquiera se puede pasar -- y el sistema usa
           'objetivo' (el Scheduler) siempre, sin pedirlo.

        Esta prueba usa cli.main(...) tal cual la usaría una persona en la
        terminal -- no ServicioSlices directamente -- para confirmar que
        (a) '--estrategia' ya no es un argumento válido, y (b) el camino
        normal CLI -> servicio -> orquestador -> Scheduler igual queda
        cableado de punta a punta sin que nadie lo pida explícitamente."""
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.correr("slice", "crear", "--nombre", "rechazado", "--tipo", "anillo", "--nodos", "3",
                       "--estrategia", "objetivo")
        c, out, err = self.correr("slice", "crear", "--nombre", "ex1obj", "--tipo", "anillo", "--nodos", "4",
                                  "--rama", "vm4:lineal:2", "--flavor", "vm2=cirros", "--flavor", "vm6=cirros",
                                  "--internet", "vm1", "--internet", "vm3", "--zona", "az-lab",
                                  "--capacidad", "vm3=vcpu:2,ram:1024,disco:8", "--desplegar")
        self.assertEqual(c, 0, err)
        self.assertIn("activo", out)
        resultado = vm_placement.obtener_ultimo_resultado()
        self.assertIsNotNone(resultado)   # confirma que de verdad pasó por el Scheduler, sin pedirlo

    def test_driver_openstack_sin_variables_de_entorno_da_error_claro(self):
        """Hallazgo del 12-nov (rúbrica oficial EX2, R1B: 'ser independiente de
        la capa de infraestructura'): antes de este cambio, la CLI siempre
        usaba el driver Linux simulado, sin forma de pedirle el driver
        OpenStack real. Esta prueba confirma que '--driver openstack' ya es
        aceptado por la CLI, y que si faltan las variables de entorno de
        '. .env.real' el error es claro, sin traza, y NUNCA intenta tocar la
        red (no se cuelga ni expone nada)."""
        claves = ("DOMAIN_ID", "ADMIN_PROJECT_ID", "ADMIN_USER_ID",
                  "ADMIN_USER_PASSWORD", "ADMIN_ROLE_ID")
        respaldo = {k: os.environ.pop(k, None) for k in claves}
        try:
            salida, errores = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(errores):
                codigo = cli.main(["--estado", self.ruta, "-u", "ana", "--driver", "openstack",
                                  "slice", "listar"])
        finally:
            for k, v in respaldo.items():
                if v is not None:
                    os.environ[k] = v
        self.assertEqual(codigo, 1)
        self.assertIn("variables de entorno", errores.getvalue())
        self.assertNotIn("Traceback", errores.getvalue())

    def test_cli_openstack_arma_el_servicio_con_el_driver_y_el_inventario_reales(self):
        """Confirma que, con las variables de entorno presentes, '--driver
        openstack' de verdad conecta el ServicioSlices con un DriverOpenStack
        en modo REAL y con el inventario que devuelve driver.descubrir_hosts()
        -- no con el inventario simulado fijo que usa 'linux'. No toca la red:
        se simula la respuesta de descubrir_hosts() con un mock."""
        from slice_orchestrator.driver_openstack import DriverOpenStack, ModoOpenStack
        from slice_orchestrator.models import Host

        hosts_falsos = [Host("worker9", "worker9", "openstack://az-real:worker9", 4, 3935,
                             "az-real", capacidad_disco_gb=9.0)]
        entorno = {"DOMAIN_ID": "d", "ADMIN_PROJECT_ID": "p", "ADMIN_USER_ID": "u",
                  "ADMIN_USER_PASSWORD": "x", "ADMIN_ROLE_ID": "r"}
        respaldo = {k: os.environ.get(k) for k in entorno}
        os.environ.update(entorno)
        try:
            with patch.object(DriverOpenStack, "descubrir_hosts", return_value=hosts_falsos):
                svc = cli._construir_servicio_openstack_real(self.ruta)
        finally:
            for k, v in respaldo.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertIsInstance(svc._driver, DriverOpenStack)
        self.assertEqual(svc._driver.modo, ModoOpenStack.REAL)
        self.assertEqual(svc._hosts_base, hosts_falsos)

    def test_cli_mantenimiento_rechazado_con_el_driver_linux(self):
        """El driver linux (simulado, el que usa la CLI por defecto) no tiene
        limpiar_residuos: la CLI debe fallar con un mensaje claro y código 1,
        no con una traza. 'mantenimiento' no exige --usuario (no es una
        operación de un usuario sobre sus propios slices)."""
        c, _, err = self.correr("mantenimiento", "limpiar-residuos", usuario=None)
        self.assertEqual(c, 1)
        self.assertIn("OpenStack real", err)
        self.assertNotIn("Traceback", err)

    def test_cli_mantenimiento_limpiar_residuos_con_driver_openstack(self):
        """Extremo a extremo (DriverOpenStack mockeado, sin tocar la red):
        '--driver openstack mantenimiento limpiar-residuos --confirmar' pasa
        confirmar=True hasta el driver y muestra el reporte que devuelve."""
        from slice_orchestrator.driver_openstack import DriverOpenStack
        from slice_orchestrator.models import Host

        hosts_falsos = [Host("worker9", "worker9", "openstack://az-real:worker9", 4, 3935, "az-real")]
        reporte_falso = {"proyectos": [{"id": "p1", "name": "orq_viejo"}],
                         "redes": [{"id": "r1", "name": "br-viejo-0-vlan10"}],
                         "flavors": []}
        entorno = {"DOMAIN_ID": "d", "ADMIN_PROJECT_ID": "p", "ADMIN_USER_ID": "u",
                  "ADMIN_USER_PASSWORD": "x", "ADMIN_ROLE_ID": "r"}
        respaldo = {k: os.environ.get(k) for k in entorno}
        os.environ.update(entorno)
        try:
            with patch.object(DriverOpenStack, "descubrir_hosts", return_value=hosts_falsos), \
                 patch.object(DriverOpenStack, "limpiar_residuos", return_value=reporte_falso) as m:
                salida, errores = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(errores):
                    codigo = cli.main(["--estado", self.ruta, "--driver", "openstack",
                                       "mantenimiento", "limpiar-residuos", "--confirmar"])
        finally:
            for k, v in respaldo.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(codigo, 0)
        m.assert_called_once_with(confirmar=True, ids_protegidos=set())
        self.assertIn("orq_viejo", salida.getvalue())
        self.assertIn("br-viejo-0-vlan10", salida.getvalue())
        self.assertIn("BORRADOS", salida.getvalue())


if __name__ == "__main__":
    unittest.main()
