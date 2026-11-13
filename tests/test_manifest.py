import unittest
from nes_static import project as manifest
from nes_static.support import Error


def rom(mapper=0, battery=False, nes2=False):
    header = bytearray(b'NES\x1a' + bytes(12))
    header[4] = 1; header[5] = 1
    header[6] = (mapper & 15) << 4 | int(battery) << 1
    header[7] = mapper & 240 | (8 if nes2 else 0)
    header[8] = mapper >> 8 if nes2 else 0
    return bytes(header) + bytes(16384 + 8192)


class ManifestTests(unittest.TestCase):
    def test_mapper_bits(self):
        for mapper in (0, 1, 4, 255, 256, 517):
            self.assertEqual(manifest.inspect(rom(mapper, nes2=True)).mapper, mapper)

    def test_truncation(self):
        with self.assertRaisesRegex(Error, 'length'): manifest.inspect(rom()[:-1])

    def test_unsupported_battery(self):
        with self.assertRaisesRegex(Error, 'Battery'): manifest.inspect(rom(19, True))
        self.assertTrue(manifest.inspect(rom(1, True, True)).battery)

    def test_special_hardware(self):
        data = bytearray(rom(nes2=True)); data[15] = 8
        with self.assertRaisesRegex(Error, 'peripherals'): manifest.inspect(data)
        data[15] = 0; data[7] |= 1
        with self.assertRaisesRegex(Error, 'console'): manifest.inspect(data)

    def test_dirty_ines(self):
        data = bytearray(rom()); data[12:16] = b'Dude'
        with self.assertRaisesRegex(Error, 'dirty'): manifest.inspect(data)

    def test_trainer_and_exponent_size(self):
        data = bytearray(rom(nes2=True)); data[6] |= 4
        self.assertEqual(manifest.inspect(data + bytes(512)).prg_bytes, 16384)
        data = bytearray(rom(nes2=True)); data[4] = 14 << 2; data[9] = 15
        self.assertEqual(manifest.inspect(data).prg_bytes, 16384)

if __name__ == '__main__': unittest.main()
