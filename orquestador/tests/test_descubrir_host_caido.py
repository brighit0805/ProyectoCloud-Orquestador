"""Un host Linux inalcanzable queda inactivo; los demás se siguen descubriendo."""

import unittest

from slice_orchestrator.driver_linux import DriverLinux, ModoDriver
from slice_orchestrator.models import Host


class _Dom:
    def info(self):
        return (1, 524288, 524288, 1, 0)


class _Conn:
    def listDomainsID(self):
        return [1, 2]

    def lookupByID(self, _):
        return _Dom()

    def close(self):
        pass


class _Libvirt:
    class libvirtError(Exception):
        pass


class _DriverFalso(DriverLinux):
    def _libvirt(self):
        return _Libvirt

    def _conectar(self, uri):
        if "192.168.201.4" in uri:
            raise ConnectionError("no route to host")
        return _Conn()


class TestDescubrirConHostCaido(unittest.TestCase):
    def test_host_caido_queda_inactivo_y_los_demas_se_leen(self):
        hosts = [Host(n, n, f"qemu+ssh://ubuntu@192.168.201.{i}/system", 16, 32768, "az-lab")
                 for i, n in ((1, "server1"), (4, "server4"))]
        d = _DriverFalso(modo=ModoDriver.REAL, hosts_conocidos=hosts)
        r = {h.id: h for h in d.descubrir_hosts()}
        self.assertTrue(r["server1"].activo)
        self.assertEqual((r["server1"].vcpu_usados, r["server1"].ram_usados_mb), (2, 1024))
        self.assertFalse(r["server4"].activo)
        self.assertEqual(r["server4"].zona_disponibilidad, "az-lab")


class _ConnConInfo(_Conn):
    def getInfo(self):
        return ["x86_64", 3935, 4, 2400, 1, 1, 4, 1]    # modelo, RAM MB, CPUs, ...


class _DriverRAMReal(_DriverFalso):
    def _conectar(self, uri):
        return _ConnConInfo()


class TestCapacidadRamReal(unittest.TestCase):
    def test_usa_la_ram_fisica_del_host(self):
        h = Host("server1", "server1", "qemu+ssh://ubuntu@192.168.201.1/system", 16, 32768, "az-lab")
        r = _DriverRAMReal(modo=ModoDriver.REAL, hosts_conocidos=[h]).descubrir_hosts()[0]
        self.assertEqual(r.capacidad_ram_mb, 3935)
        self.assertEqual(r.capacidad_vcpu, 16)       # solo la RAM se corrige

    def test_si_no_se_puede_leer_se_conserva_la_configurada(self):
        h = Host("server1", "server1", "qemu+ssh://ubuntu@192.168.201.1/system", 16, 32768, "az-lab")
        r = _DriverFalso(modo=ModoDriver.REAL, hosts_conocidos=[h]).descubrir_hosts()[0]
        self.assertEqual(r.capacidad_ram_mb, 32768)


if __name__ == "__main__":
    unittest.main()
