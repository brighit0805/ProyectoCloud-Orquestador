"""Pruebas del monitoreo de uso REAL (CPU/RAM por host y por VM)."""

import contextlib
import io
import json
import unittest

from slice_orchestrator import cli
from slice_orchestrator.monitoreo_uso import (ErrorMedicion, MedidorUso, destino_ssh,
                                              interpretar, parsear_domstats,
                                              script_remoto)
from slice_orchestrator.servicio_slices import ErrorServicio, ServicioSlices


def salida_remota(extra_m2=""):
    """Dos muestras separadas por 5 s. vm1 usa 1 vCPU al 80 %; vm2 informa su RAM
    por el balloon del invitado; vm3 solo aparece en la 2ª muestra (se ignora)."""
    def dom(nombre, cpu_ns, balloon):
        return (f"Domain: '{nombre}'\n  cpu.time={cpu_ns}\n  vcpu.current=1\n"
                f"  balloon.maximum=1048576\n{balloon}")
    m1 = ("##T 100.0\ncpu  1000 0 500 8000 500 0 0 0 0 0\n##DOM\n"
          + dom("vm1", 1_000_000_000, "  balloon.rss=204800\n")
          + dom("vm2", 0, "  balloon.available=1000000\n  balloon.unused=400000\n  balloon.rss=999\n")
          + "##FIN\n")
    m2 = ("##T 105.0\ncpu  1500 0 700 8200 600 0 0 0 0 0\n##DOM\n"
          + dom("vm1", 5_000_000_000, "  balloon.rss=819200\n")
          + dom("vm2", 0, "  balloon.available=1000000\n  balloon.unused=400000\n  balloon.rss=999\n")
          + dom("vm3", 7, "  balloon.rss=1\n") + extra_m2 + "##FIN\n")
    return (f"##M1\n{m1}##M2\n{m2}##MEM\nMemTotal:        4194304 kB\n"
            "MemAvailable:    2097152 kB\n")


class TestInterpretar(unittest.TestCase):
    def test_host_y_vms(self):
        u = interpretar(salida_remota(), "server2")
        self.assertEqual(u.cpu_pct, 70.0)             # 1 - 300/1000
        self.assertEqual((u.ram_total_mb, u.ram_usada_mb), (4096.0, 2048.0))
        self.assertEqual([v.dominio for v in u.vms], ["vm1", "vm2"])   # vm3 no estaba en la 1ª muestra
        vm1, vm2 = u.vms
        self.assertEqual(vm1.cpu_pct, 80.0)           # 4e9 ns en 5 s con 1 vCPU
        self.assertEqual((vm1.ram_asignada_mb, vm1.ram_usada_mb, vm1.ram_origen), (1024.0, 800.0, "rss"))
        self.assertEqual((vm2.ram_usada_mb, vm2.ram_origen), (585.9, "invitado"))

    def test_salida_basura_da_error_claro(self):
        with self.assertRaises(ErrorMedicion):
            interpretar("Permission denied", "server2")

    def test_domstats(self):
        d = parsear_domstats("Domain: 'a'\n  cpu.time=5\n  x=notanumber\nDomain: 'b'\n  cpu.time=7\n")
        self.assertEqual(d, {"a": {"cpu.time": 5.0}, "b": {"cpu.time": 7.0}})


class TestDestinosYScript(unittest.TestCase):
    def test_destino_linux_y_openstack(self):
        self.assertEqual(destino_ssh("server2", "qemu+ssh://ubuntu@192.168.201.2/system", {}),
                         "ubuntu@192.168.201.2")
        self.assertEqual(destino_ssh("worker1", "openstack://nova:worker1", {}), "ubuntu@worker1")
        env = {"ORQ_MONITOR_HOSTS": "worker1=10.0.0.11,worker2=10.0.0.12", "ORQ_SSH_USUARIO": "root"}
        self.assertEqual(destino_ssh("worker2", "openstack://nova:worker2", env), "root@10.0.0.12")

    def test_script_toma_dos_muestras(self):
        s = script_remoto(7)
        self.assertIn("sleep 7.0", s)
        self.assertEqual(s.count("sleep"), 1)
        self.assertLess(s.index("##M1"), s.index("sleep"))
        self.assertLess(s.index("sleep"), s.index("##M2"))
        self.assertIn("domstats", s)


class TestServicio(unittest.TestCase):
    def _svc(self, ejecutar):
        return ServicioSlices(medidor_uso=MedidorUso(ejecutar))

    def test_mide_todos_los_hosts_a_la_vez(self):
        llamadas = []

        def ejecutar(destino, script, intervalo):
            llamadas.append(destino)
            return salida_remota()
        r = self._svc(ejecutar).monitoreo_uso(5)
        self.assertEqual(len(r["hosts"]), len(llamadas))
        h = r["hosts"][0]
        self.assertEqual(h["cpu_pct"], 70.0)
        self.assertEqual(h["vms"][0]["cpu_pct"], 80.0)
        self.assertEqual(h["vcpu_asignadas"], 0)

    def test_un_host_caido_no_impide_medir_los_demas(self):
        def ejecutar(destino, script, intervalo):
            if "worker2" in destino:
                raise ErrorMedicion("SSH a worker2 falló")
            return salida_remota()
        r = self._svc(ejecutar).monitoreo_uso(5)
        errores = [h for h in r["hosts"] if "error" in h]
        self.assertEqual([h["host"] for h in errores], ["worker2"])
        self.assertTrue(all("cpu_pct" in h for h in r["hosts"] if "error" not in h))

    def test_simulado_sin_medidor_explica_que_hace_falta_driver_real(self):
        with self.assertRaises(ErrorServicio) as cm:
            ServicioSlices().monitoreo_uso(5)
        self.assertIn("driver real", str(cm.exception))

    def test_intervalo_invalido(self):
        with self.assertRaises(ErrorServicio):
            self._svc(lambda *a: salida_remota()).monitoreo_uso(0)

    def test_nombres_legibles_con_driver_openstack(self):
        class Drv:
            modo = "real"

            def nombres_de_instancias(self):
                return {"vm1": "Ex2_G02_test-vm1"}
        svc = ServicioSlices(driver=Drv(), medidor_uso=MedidorUso(lambda *a: salida_remota()))
        vms = svc.monitoreo_uso(5)["hosts"][0]["vms"]
        self.assertEqual(vms[0]["vm"], "Ex2_G02_test-vm1")
        self.assertEqual(vms[1]["vm"], "vm2")        # sin nombre conocido, queda el del hipervisor


class TestCLI(unittest.TestCase):
    def _correr(self, *args):
        svc = ServicioSlices(medidor_uso=MedidorUso(lambda *a: salida_remota()))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(list(args), servicio=svc)
        return rc, buf.getvalue()

    def test_tabla(self):
        rc, out = self._correr("monitoreo", "--intervalo", "5")
        self.assertEqual(rc, 0)
        for esperado in ("CPU real", "70.0%", "Por VM:", "80.0", "vm1"):
            self.assertIn(esperado, out)

    def test_json(self):
        rc, out = self._correr("--json", "monitoreo")
        self.assertEqual(rc, 0)
        self.assertIn("hosts", json.loads(out))


if __name__ == "__main__":
    unittest.main()
