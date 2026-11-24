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


if __name__ == "__main__":
    unittest.main()
