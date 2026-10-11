from unittest import TestCase

import numpy as np
from PIL import Image

from depth_motion import make_depth_layers


class DepthMotionTests(TestCase):
    def test_depth_layers_are_export_sized_and_transparent(self):
        source = Image.new("RGB", (160, 90), "navy")
        depth = Image.fromarray(np.tile(np.arange(160, dtype=np.uint8), (90, 1)), mode="L")
        background, middle, near = make_depth_layers(source, depth, (320, 180))

        self.assertEqual(background.size, (320, 180))
        self.assertEqual(middle.mode, "RGBA")
        self.assertEqual(near.mode, "RGBA")
        self.assertLess(middle.getchannel("A").getextrema()[0], 255)
        self.assertGreater(near.getchannel("A").getextrema()[1], 0)
