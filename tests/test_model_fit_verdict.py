"""Will a model actually run well on this box — the arithmetic behind the row.

`model_fit.assess` answers a different question from `fits_now`: not "is this
already-configured backend safe to route to right now", but "is this model in
the store worth downloading and running at all". Its answer is one machine
token, worded by frontend/src/lib/modelFit.ts.

The failure this guards is asymmetric, and the tests are weighted for it. A
missed warning costs the owner an hour and a disk. A WRONG warning costs them a
model that would have run fine — and they have no way to check, because the
whole point is that they have not run it yet. So: never speak without evidence,
and when the evidence is thin, say `unknown` (which renders as silence) rather
than guess.
"""
import unittest

from ava_bridge import model_fit


class TheVerdict(unittest.TestCase):
    def test_a_model_with_room_to_spare_just_fits(self):
        self.assertEqual(model_fit.assess(9.7, 16, "vram")["verdict"], "should_fit")

    def test_a_model_past_the_pool_spills_on_a_discrete_card(self):
        # It fits in the machine, not in the accelerator: some layers page
        # through system RAM on every token. Slow, but it runs.
        self.assertEqual(model_fit.assess(22.4, 16, "vram")["verdict"], "will_spill")

    def test_a_model_far_past_the_pool_is_not_worth_trying(self):
        # At more than _SPILL_LIMIT x the pool almost nothing stays resident —
        # that is not a slow model, it is a progress bar.
        self.assertEqual(model_fit.assess(57.8, 16, "vram")["verdict"], "wont_fit")

    def test_the_boundary_is_a_ratio_not_a_constant(self):
        """A big box tolerates a bigger overflow, because the ratio is what hurts."""
        self.assertEqual(model_fit.assess(150, 128, "vram")["verdict"], "will_spill")
        self.assertEqual(model_fit.assess(300, 128, "vram")["verdict"], "wont_fit")

    def test_headroom_is_reported_signed(self):
        # Negative headroom IS the overflow, and the UI may want to show it.
        self.assertEqual(model_fit.assess(22.4, 16, "vram")["headroom_gb"], -6.4)
        self.assertEqual(model_fit.assess(9.7, 16, "vram")["headroom_gb"], 6.3)

    def test_every_verdict_is_in_the_closed_vocabulary(self):
        """Without this the vocabulary is a comment. The frontend renders an
        unrecognised token as SILENCE, so a stray verdict would go quiet rather
        than wrong — invisible in exactly the case the feature exists for."""
        cases = [(9.7, 16, "vram"), (22.4, 16, "vram"), (57.8, 16, "vram"),
                 (None, 16, "vram"), (9.7, None, None), (9.7, 0, "vram"),
                 (57.8, 16, "unified"), (9.7, 16, "system"), (1, 16, None)]
        for need, pool, kind in cases:
            with self.subTest(need=need, pool=pool, kind=kind):
                self.assertIn(model_fit.assess(need, pool, kind)["verdict"],
                              model_fit.FIT_VERDICTS)


class SpillOnlyExistsWhereThereIsSomewhereToSpill(unittest.TestCase):
    """A unified pool has one pool. "Runs partly on the processor" describes a
    boundary that does not exist there — the CPU and GPU are reading the same
    silicon — so a Spark or an Apple box must never be told its model spills."""

    def test_a_unified_box_never_spills(self):
        for need in (17, 22.4, 31):
            with self.subTest(need=need):
                v = model_fit.assess(need, 16, "unified")["verdict"]
                self.assertEqual(v, "wont_fit")
                self.assertNotEqual(v, "will_spill")

    def test_nor_does_a_cpu_only_box(self):
        self.assertEqual(model_fit.assess(22.4, 16, "system")["verdict"], "wont_fit")

    def test_an_observed_spill_is_ignored_on_a_unified_pool(self):
        # Even a reading has to mean something. Whatever produced `spilled` on a
        # unified box was not measuring what this word means.
        self.assertNotEqual(
            model_fit.assess(9.7, 16, "unified", spilled=True)["verdict"], "will_spill")


class ObservationOutranksArithmetic(unittest.TestCase):
    def test_a_watched_spill_is_reported_as_measured(self):
        # The arithmetic says this fits. We watched it spill anyway — quantised
        # KV cache, a second model resident, a context longer than anyone
        # predicted. The measurement wins, and says so.
        got = model_fit.assess(9.7, 16, "vram", spilled=True)
        self.assertEqual(got["verdict"], "will_spill")
        self.assertTrue(got["measured"])
        self.assertEqual(got["source"], "observed")

    def test_a_derived_verdict_never_claims_to_be_measured(self):
        for need in (9.7, 22.4, 57.8):
            with self.subTest(need=need):
                got = model_fit.assess(need, 16, "vram")
                self.assertFalse(got["measured"])
                self.assertEqual(got["source"], "derived")


class WhenItCannotTell(unittest.TestCase):
    def test_no_size_means_unknown_not_a_guess(self):
        self.assertEqual(model_fit.assess(None, 16, "vram")["verdict"], "unknown")

    def test_no_readable_pool_means_unknown(self):
        # A box whose memory Ava cannot read gets no verdict at all, rather than
        # a verdict against a number nobody measured.
        self.assertEqual(model_fit.assess(9.7, None, None)["verdict"], "unknown")
        self.assertEqual(model_fit.assess(9.7, 0, "vram")["verdict"], "unknown")


class SizingFromAName(unittest.TestCase):
    """Last resort, for a model not downloaded yet. Reads a parameter count and
    a quantisation hint and NOTHING else — no table of known model names, the
    same rule hardware.py's `_short_model_name` follows."""

    def test_reads_the_parameter_count(self):
        self.assertAlmostEqual(model_fit.size_from_id("llama3.1:8b", "ollama"), 4.8, 1)
        self.assertAlmostEqual(model_fit.size_from_id("qwen2.5:72b", "ollama"), 43.2, 1)

    def test_multiplies_out_a_mixture_of_experts(self):
        self.assertAlmostEqual(model_fit.size_from_id("mixtral:8x7b", "ollama"), 33.6, 1)

    def test_reads_the_quantisation_the_name_declares(self):
        awq = model_fit.size_from_id("Qwen/Qwen3-8B-AWQ", "vllm")
        fp16 = model_fit.size_from_id("Qwen/Qwen3-8B", "vllm")
        self.assertLess(awq, fp16)

    def test_falls_back_to_what_the_engine_actually_ships(self):
        """`llama3.1:8b` is a 4.9 GB Q4 in Ollama and a ~16 GB fp16 on the Hub.
        Reading the Ollama tag at half precision called it 16.8 GB and would
        have warned about a model that fits a laptop with room to spare."""
        self.assertLess(model_fit.size_from_id("llama3.1:8b", "ollama"),
                        model_fit.size_from_id("meta/Llama-3.1-8B", "vllm"))

    def test_says_nothing_when_the_name_says_nothing(self):
        # None, not a default — this is what makes the row silent instead of
        # wrong. A guess with no evidence is exactly what `unknown` is for.
        for name in ("some/private-model", "", None, "my-finetune"):
            with self.subTest(name=name):
                self.assertIsNone(model_fit.size_from_id(name, "vllm"))

    def test_refuses_a_number_that_is_not_a_parameter_count(self):
        # A version, a date or a context length must not become a model size.
        self.assertIsNone(model_fit.size_from_id("model-9000b", "vllm"))


if __name__ == "__main__":
    unittest.main()
