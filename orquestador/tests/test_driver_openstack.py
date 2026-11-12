"""Pruebas del DriverOpenStack con el OpenStack simulado en memoria (sin clúster).

Verifican el flujo de peticiones (Keystone -> Neutron -> Nova), la idempotencia,
los reintentos y el rollback. NO prueban el clúster real: eso se valida aparte.
"""

import unittest

from slice_orchestrator import direccionamiento
from slice_orchestrator.driver_base import DriverPlataforma
from slice_orchestrator.driver_linux import DriverLinux
from slice_orchestrator.driver_openstack import (
    ConfigOpenStack,
    DriverOpenStack,
    DriverOpenStackError,
    ModoOpenStack,
    SesionSimulada,
    _Resp,
    parsear_ubicacion,
    ubicacion,
)
from slice_orchestrator.models import EstadoSlice, TipoTopologia
from slice_orchestrator.network_manager import VLANAllocator
from slice_orchestrator.orchestrator import (
    DespliegueFallidoError,
    desplegar_slice_existente,
    eliminar_slice,
)
from slice_orchestrator.topology_manager import construir_ex1_topology, construir_topologia
from slice_orchestrator.vm_placement import EstrategiaPlacement


class _SesionPlacementFalsa:
    """Sesion falsa minima: solo responde lo que _consultar_placement() y
    descubrir_hosts() necesitan (autenticación, zonas, hipervisores y los
    tres endpoints de Placement), con datos fabricados a mano para probar
    el caso real encontrado contra el clúster (ver test de arriba)."""

    def request(self, metodo, url, headers=None, json=None, params=None, timeout=None):
        if url.endswith("/v3/auth/tokens"):
            return _Resp(201, {"token": {}}, {"X-Subject-Token": "tok-1"})
        if url.endswith("/os-availability-zone/detail"):
            return _Resp(200, {"availabilityZoneInfo": []})
        if url.endswith("/os-hypervisors/detail"):
            return _Resp(200, {"hypervisors": [
                {"hypervisor_hostname": "worker1", "vcpus": 4, "memory_mb": 3935,
                 "vcpus_used": 1, "memory_mb_used": 1024, "local_gb": 9, "local_gb_used": 4},
            ]})
        if url.endswith("/resource_providers") and params == {"name": "worker1"}:
            return _Resp(200, {"resource_providers": [{"uuid": "uuid-w1", "name": "worker1"}]})
        if url.endswith("/resource_providers/uuid-w1/inventories"):
            return _Resp(200, {"inventories": {
                "VCPU": {"total": 4, "reserved": 0, "allocation_ratio": 16.0},
                "MEMORY_MB": {"total": 3935, "reserved": 512, "allocation_ratio": 1.5},
                "DISK_GB": {"total": 9, "reserved": 0, "allocation_ratio": 1.0},
            }})
        if url.endswith("/resource_providers/uuid-w1/usages"):
            return _Resp(200, {"usages": {"VCPU": 2, "MEMORY_MB": 1024, "DISK_GB": 8}})
        raise AssertionError(f"ruta no simulada en la prueba: {metodo} {url}")


def driver_simulado(**kw):
    cfg = ConfigOpenStack(dominio_id="dom", proyecto_admin_id="adm", usuario_admin_id="u-admin",
                          password_admin="x", rol_admin_id="rol-admin")
    d = DriverOpenStack(config=cfg, imagen_id_defecto="img-cirros", flavor_id_defecto="flv-512",
                        espera_reintento_s=0.0, **kw)
    d.sesion = SesionSimulada(cfg)
    return d


def anillo3():
    return construir_topologia("g2", "anillo", "grupo2@pucp.edu.pe", TipoTopologia.ANILLO, 3)


def desplegar(slc, driver, **kw):
    hosts = driver.descubrir_hosts()
    return desplegar_slice_existente(slc, hosts, driver, VLANAllocator(),
                                     EstrategiaPlacement.ROUND_ROBIN, **kw), hosts


class TestContratoYConfiguracion(unittest.TestCase):
    def test_ambos_drivers_cumplen_el_contrato(self):
        self.assertIsInstance(DriverOpenStack(), DriverPlataforma)
        self.assertIsInstance(DriverLinux(), DriverPlataforma)

    def test_ubicacion_ida_y_vuelta(self):
        self.assertEqual(ubicacion("az1", "worker2"), "openstack://az1:worker2")
        self.assertEqual(parsear_ubicacion("openstack://az1:worker2"), ("az1", "worker2"))
        self.assertEqual(parsear_ubicacion("openstack://az1"), ("az1", None))

    def test_config_desde_entorno_y_faltantes(self):
        cfg = ConfigOpenStack.desde_entorno({"ACCESS_NODE_IP": "10.0.0.1", "DOMAIN_ID": "d"})
        self.assertEqual(cfg.keystone, "http://10.0.0.1:55000/v3")
        self.assertIn("ADMIN_USER_PASSWORD", cfg.faltantes())
        self.assertNotIn("secreto", repr(ConfigOpenStack(password_admin="secreto")))

    def test_descubrir_hosts_lee_workers_y_zona(self):
        hosts = driver_simulado().descubrir_hosts()
        self.assertEqual([h.id for h in hosts], ["worker1", "worker2", "worker3"])
        self.assertEqual(hosts[0].uri_libvirt, "openstack://nova:worker1")
        self.assertEqual((hosts[0].capacidad_vcpu, hosts[0].zona_disponibilidad), (4, "nova"))

    def test_descubrir_hosts_lee_disco_real_de_nova(self):
        """Lab 7: el disco real (local_gb/local_gb_used de Nova) debe llegar
        a Host.capacidad_disco_gb/disco_usado_gb para que el
        FiltroCapacidadDisco del scheduler deje de estar deshabilitado."""
        d = driver_simulado()
        for h in d.sesion.hipervisores:
            h["local_gb"] = 9
            h["local_gb_used"] = 8 if h["hypervisor_hostname"] == "worker3" else 0
        hosts = {h.id: h for h in d.descubrir_hosts()}
        self.assertEqual((hosts["worker3"].capacidad_disco_gb, hosts["worker3"].disco_usado_gb), (9.0, 8.0))
        self.assertEqual(hosts["worker3"].disco_disponible_gb, 1.0)
        self.assertEqual((hosts["worker1"].capacidad_disco_gb, hosts["worker1"].disco_usado_gb), (9.0, 0.0))

    def test_descubrir_hosts_sin_disco_informado_mantiene_filtro_desactivado(self):
        """Si Nova no informa local_gb (queda en 0 por defecto en la
        simulación), el host sigue sin disco informado, igual que antes."""
        hosts = driver_simulado().descubrir_hosts()
        self.assertEqual(hosts[0].capacidad_disco_gb, 0.0)

    def test_descubrir_hosts_usa_placement_como_fuente_de_verdad(self):
        """Lab 7 (hallazgo real contra el clúster, confirmado con German):
        Nova no decide con os-hypervisors/detail, decide con el servicio
        Placement. Si Placement reporta mas disco usado del que dice la
        vista administrativa vieja de Nova (ej. por VMs previas cuya
        asignación no se refleja igual en ambas vistas), el Host debe
        reflejar el numero de Placement, no el de os-hypervisors/detail."""
        cfg = ConfigOpenStack(dominio_id="dom", proyecto_admin_id="adm", usuario_admin_id="u-admin",
                              password_admin="x", rol_admin_id="rol-admin")
        d = DriverOpenStack(config=cfg, espera_reintento_s=0.0)
        d.sesion = _SesionPlacementFalsa()
        hosts = {h.id: h for h in d.descubrir_hosts()}
        w1 = hosts["worker1"]
        # os-hypervisors/detail (respaldo) decia local_gb=9/local_gb_used=4
        # -> 5 GB libres; Placement (fuente real) dice 9 total con 8 usados
        # -> solo 1 GB libre real. Debe ganar Placement.
        self.assertEqual((w1.capacidad_disco_gb, w1.disco_usado_gb), (9.0, 8.0))
        self.assertEqual(w1.disco_disponible_gb, 1.0)

    def test_descubrir_hosts_sin_placement_cae_a_os_hypervisors(self):
        """Si Placement no responde (404, red caída, etc.), descubrir_hosts
        no debe fallar: debe seguir usando os-hypervisors/detail como antes
        (comportamiento de respaldo, ya probado por los tests anteriores
        contra driver_simulado(), cuya SesionSimulada no implementa Placement)."""
        hosts = driver_simulado().descubrir_hosts()
        self.assertEqual([h.id for h in hosts], ["worker1", "worker2", "worker3"])


class TestDespliegueCompleto(unittest.TestCase):
    def test_anillo_se_despliega_con_el_orquestador_sin_cambios(self):
        d = driver_simulado()
        slc, _ = desplegar(anillo3(), d)
        sim = d.sesion
        self.assertEqual(slc.estado, EstadoSlice.ACTIVO)
        self.assertEqual(len(sim.proyectos), 1)
        self.assertEqual(len(sim.servidores), 3)
        redes_slice = [r for r in sim.redes.values() if r["name"] != "external"]
        self.assertEqual((len(redes_slice), len(sim.subredes)), (3, 3))
        self.assertEqual(len(sim.puertos), 6)            # 3 VMs x 2 enlaces

    def test_puertos_usan_mac_e_ip_del_direccionamiento(self):
        d = driver_simulado()
        slc, _ = desplegar(anillo3(), d)
        for nodo in slc.nodos:
            for itf in direccionamiento.interfaces_de(slc, nodo):
                p = next(p for p in d.sesion.puertos.values()
                         if p["name"] == f"{nodo.nombre}:{itf.red}")
                self.assertEqual(p["mac_address"], itf.mac)
                self.assertEqual(p["fixed_ips"][0]["ip_address"], itf.cidr.split("/")[0])
                self.assertFalse(p["port_security_enabled"])

    def test_la_instancia_va_al_worker_que_decidio_placement(self):
        d = driver_simulado()
        slc, _ = desplegar(anillo3(), d)
        esperado = {n.nombre: f"nova:{n.host_id}" for n in slc.nodos}
        obtenido = {s["name"]: s["availability_zone"] for s in d.sesion.servidores.values()}
        self.assertEqual(obtenido, esperado)
        self.assertEqual(set(esperado.values()), {"nova:worker1", "nova:worker2", "nova:worker3"})

    def test_ex1_topology_con_acceso_a_internet_crea_puerto_en_external(self):
        d = driver_simulado()
        slc = construir_ex1_topology("ex1", "ex1", "grupo2@pucp.edu.pe", "nova")
        hosts = d.descubrir_hosts()
        cfg = {"ips": {"vm1": "10.60.110.21", "vm3": "10.60.110.22"}, "prefijo": 24,
               "gateway": "10.60.110.1", "vlan": None}
        desplegar_slice_existente(slc, hosts, d, VLANAllocator(),
                                  EstrategiaPlacement.ROUND_ROBIN, config_acceso=cfg)
        self.assertEqual(len(d.sesion.servidores), 6)
        con_acceso = [p for p in d.sesion.puertos.values() if p["name"].endswith(":acceso")]
        self.assertGreaterEqual(len(con_acceso), 1)
        self.assertTrue(all(p["network_id"] == "net-external" for p in con_acceso))

    def test_eliminar_slice_no_deja_recursos_ni_proyecto(self):
        d = driver_simulado()
        slc, hosts = desplegar(anillo3(), d)
        eliminar_slice(slc, hosts, d, VLANAllocator())
        sim = d.sesion
        self.assertEqual((len(sim.servidores), len(sim.puertos), len(sim.subredes)), (0, 0, 0))
        self.assertEqual([r["name"] for r in sim.redes.values()], ["external"])
        self.assertEqual(len(sim.proyectos), 0)
        self.assertEqual(slc.estado, EstadoSlice.ELIMINADO)

    def test_se_registran_los_comandos_que_se_enviarian(self):
        d = driver_simulado()
        desplegar(anillo3(), d)
        firmas = {(m, r) for m, r, _ in d.sesion.comandos}
        for esperado in [("POST", "/v3/auth/tokens"), ("POST", "/v3/projects"),
                         ("POST", "/v2.0/networks"), ("POST", "/v2.0/subnets"),
                         ("POST", "/v2.0/ports"), ("POST", "/v2.1/servers")]:
            self.assertIn(esperado, firmas)


class TestIdempotenciaYErrores(unittest.TestCase):
    def preparado(self):
        d = driver_simulado()
        slc = anillo3()
        d.proyecto_id = d._api("admin", "POST", d.config.keystone + "/projects", esperado=(201,),
                               cuerpo={"project": {"name": "p", "domain_id": "dom"}}).json()["project"]["id"]
        return d, slc

    def test_crear_red_dos_veces_crea_una_sola(self):
        d, _ = self.preparado()
        a = d.crear_red("openstack://nova:worker1", "enlace-a", 3000)
        b = d.crear_red("openstack://nova:worker2", "enlace-a", 3000)
        self.assertEqual(a, b)
        self.assertEqual(len([r for r in d.sesion.redes.values() if r["name"] == "enlace-a"]), 1)

    def test_crear_vm_dos_veces_devuelve_la_misma_instancia(self):
        d, _ = self.preparado()
        d.crear_red("openstack://nova", "enlace-a", 3000)
        args = dict(host_uri="openstack://nova:worker1", nombre_vm="vm1", vcpu=1, ram_mb=512,
                    disco_gb=1.0, imagen="x.qcow2", redes=[("enlace-a", 3000)])
        self.assertEqual(d.crear_vm(**args), d.crear_vm(**args))
        self.assertEqual(len(d.sesion.servidores), 1)

    def test_sin_proyecto_las_operaciones_fallan_con_mensaje_claro(self):
        with self.assertRaises(DriverOpenStackError) as cm:
            driver_simulado().crear_red("openstack://nova", "r", 3000)
        self.assertIn("proyecto", str(cm.exception))

    def test_imagen_sin_id_en_glance_falla(self):
        d = DriverOpenStack(flavor_id_defecto="f")
        with self.assertRaises(DriverOpenStackError):
            d.asegurar_imagen("openstack://nova", "desconocida.qcow2")

    def test_error_http_inesperado_se_reporta(self):
        d, _ = self.preparado()
        d.sesion.fallos_http[("POST", "/v2.0/networks")] = [500]
        with self.assertRaises(DriverOpenStackError) as cm:
            d.crear_red("openstack://nova", "r", 3000)
        self.assertIn("HTTP 500", str(cm.exception))

    def test_token_vencido_se_renueva_solo(self):
        d, _ = self.preparado()
        d.sesion.fallos_http[("GET", "/v2.0/networks")] = [401]
        antes = len([c for c in d.sesion.comandos if c[1] == "/v3/auth/tokens"])
        d.crear_red("openstack://nova", "r", 3000)
        despues = len([c for c in d.sesion.comandos if c[1] == "/v3/auth/tokens"])
        self.assertGreater(despues, antes)

    def test_error_de_conexion_se_reintenta(self):
        class Inestable(SesionSimulada):
            fallos = 2

            def request(self, *a, **k):
                if Inestable.fallos > 0:
                    Inestable.fallos -= 1
                    raise ConnectionError("sin ruta")
                return super().request(*a, **k)

        d = driver_simulado()
        d.sesion = Inestable(d.config)
        self.assertTrue(d._admin())             # tercer intento responde

    def test_url_de_consola(self):
        d, _ = self.preparado()
        d.crear_red("openstack://nova", "enlace-a", 3000)
        d.crear_vm("openstack://nova:worker1", "vm1", 1, 512, 1.0, "x", [("enlace-a", 3000)])
        self.assertIn("vnc_auto", d.url_consola("vm1"))


class TestReintentosYRollback(unittest.TestCase):
    def test_con_reintentos_una_falla_transitoria_se_recupera(self):
        d = driver_simulado(fallar_en_operacion=4)       # falla una operación del driver
        slc, _ = desplegar(anillo3(), d, reintentos=2, espera_reintento_s=0.0)
        self.assertEqual(slc.estado, EstadoSlice.ACTIVO)
        self.assertEqual(len(d.sesion.servidores), 3)

    def test_falla_http_de_nova_se_recupera_porque_los_puertos_son_idempotentes(self):
        d = driver_simulado()
        d.sesion.fallos_http[("POST", "/v2.1/servers")] = [500]
        slc, _ = desplegar(anillo3(), d, reintentos=2, espera_reintento_s=0.0)
        self.assertEqual(slc.estado, EstadoSlice.ACTIVO)
        self.assertEqual(len(d.sesion.puertos), 6)       # no se duplicaron al reintentar

    def test_sin_reintentos_el_rollback_deja_openstack_limpio(self):
        d = driver_simulado(fallar_en_operacion=6)
        hosts = d.descubrir_hosts()
        with self.assertRaises(DespliegueFallidoError):
            desplegar_slice_existente(anillo3(), hosts, d, VLANAllocator(),
                                      EstrategiaPlacement.ROUND_ROBIN, reintentos=1)
        sim = d.sesion
        self.assertEqual((len(sim.servidores), len(sim.puertos), len(sim.subredes)), (0, 0, 0))
        self.assertEqual([r["name"] for r in sim.redes.values()], ["external"])
        self.assertEqual(len(sim.proyectos), 0)
        for h in hosts:
            self.assertEqual((h.vcpu_usados, h.ram_usados_mb), (0, 0))


class TestSincronizarCatalogoFlavors(unittest.TestCase):
    """Resolución automática de flavor_por_perfil contra Nova (el usuario
    final no necesita conocer el ID del flavor de antemano), simétrica a
    sincronizar_catalogo_imagenes."""

    def driver_sin_mapeo(self, flavores_nova=None):
        cfg = ConfigOpenStack(dominio_id="dom", proyecto_admin_id="adm",
                              usuario_admin_id="u-admin", password_admin="x",
                              rol_admin_id="rol-admin")
        d = DriverOpenStack(config=cfg, imagen_id_defecto="img-cirros")
        d.sesion = SesionSimulada(cfg)
        if flavores_nova is not None:
            d.sesion.flavores = flavores_nova
        return d

    def test_empareja_por_vcpu_ram_y_disco_suficiente(self):
        d = self.driver_sin_mapeo([
            {"id": "flv-1", "name": "m1.tiny", "vcpus": 1, "ram": 512, "disk": 4},
        ])
        mapeo = d.sincronizar_catalogo_flavors()
        # ubuntu y cirros piden ambos 1 vCPU / 512 MB en el catálogo
        self.assertEqual(mapeo[(1, 512, 2.2)], "flv-1")
        self.assertEqual(mapeo[(1, 512, 1.0)], "flv-1")

    def test_disco_insuficiente_no_empareja(self):
        d = self.driver_sin_mapeo([
            {"id": "flv-chico", "name": "m1.nano", "vcpus": 1, "ram": 512, "disk": 1},
        ])
        mapeo = d.sincronizar_catalogo_flavors()
        # el flavor de Nova tiene 1 GB de disco; ubuntu pide 2.2 GB: no encaja
        self.assertNotIn((1, 512, 2.2), mapeo)
        # cirros pide 1.0 GB: sí encaja (disco de Nova >= pedido)
        self.assertEqual(mapeo[(1, 512, 1.0)], "flv-chico")

    def test_sin_coincidencias_usa_el_primero_como_defecto(self):
        d = self.driver_sin_mapeo([
            {"id": "flv-raro", "name": "otro", "vcpus": 8, "ram": 16384, "disk": 80},
        ])
        d.sincronizar_catalogo_flavors()
        self.assertEqual(d.flavor_id_defecto, "flv-raro")

    def test_no_pisa_un_mapeo_manual_ya_puesto(self):
        d = self.driver_sin_mapeo([
            {"id": "flv-nuevo", "name": "m1.tiny", "vcpus": 1, "ram": 512, "disk": 4},
        ])
        d.flavor_por_perfil[(1, 512, 2.2)] = "id-manual"
        d.sincronizar_catalogo_flavors()
        self.assertEqual(d.flavor_por_perfil[(1, 512, 2.2)], "id-manual")

    def test_en_modo_simulado_no_sincroniza_sola(self):
        """Comportamiento previo intacto: en SIMULADO no se auto-sincroniza."""
        d = self.driver_sin_mapeo([
            {"id": "flv-1", "name": "m1.tiny", "vcpus": 1, "ram": 512, "disk": 4},
        ])
        with self.assertRaises(DriverOpenStackError):
            d._resolver_flavor(1, 512, 2.2)

    def test_modo_real_resuelve_sola_sin_mapeo(self):
        d = self.driver_sin_mapeo([
            {"id": "flv-1", "name": "m1.tiny", "vcpus": 1, "ram": 512, "disk": 4},
        ])
        d.modo = ModoOpenStack.REAL
        self.assertEqual(d._resolver_flavor(1, 512, 2.2), "flv-1")


if __name__ == "__main__":
    unittest.main()
