"""Tests for the rename / ROB simulator — branch rollback & precise exceptions."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.simulator import (  # noqa: E402
    ARCH_REGS,
    Violation,
    parse_events,
    parse_program,
    simulate,
    Simulator,
)

PROG_BRANCH = """
ADD R1,R0,R0
ADD R2,R1,R0
BEQ R1,R2 predict=taken
ADD R3,R1,R2
ADD R4,R3,R1
ADD R1,R4,R2
SUB R5,R1,R0
"""

PROG_EXC = """
ADD R1,R0,R0
ADD R2,R1,R0
MUL R3,R1,R2
ADD R4,R3,R1
SUB R5,R4,R3
"""


def run(program, events, num_phys=16):
    return simulate(parse_program(program), parse_events(events), num_phys)


def violation_code(program, events, num_phys=16):
    report = run(program, events, num_phys)
    if report["violation"] is None:
        return None, report
    return report["violation"]["code"], report


class ParseTests(unittest.TestCase):
    def test_parse_program_with_labels_and_comments(self):
        text = """
# a comment
I0: ADD R1, R0, R0
I1: BEQ R1, R0 predict=not-taken
"""
        instrs = parse_program(text)
        self.assertEqual(len(instrs), 2)
        self.assertTrue(instrs[1].is_branch)
        self.assertFalse(instrs[1].predicted_taken)

    def test_parse_events_aliases(self):
        evs = parse_events("dispatch I0\nwb 0\nresolve 0 taken\nexception 1\ncommit")
        self.assertEqual([e.kind for e in evs],
                         ["dispatch", "writeback", "resolve", "exception", "commit"])
        self.assertTrue(evs[2].taken)
        self.assertIsNone(evs[4].seq)

    def test_bad_opcode(self):
        with self.assertRaises(ValueError):
            parse_program("FOO R1,R0,R0")

    def test_register_out_of_range(self):
        with self.assertRaises(ValueError):
            parse_program("ADD R8,R0,R0")

    def test_label_misorder(self):
        with self.assertRaises(ValueError):
            parse_program("I1: ADD R1,R0,R0")

    def test_limits(self):
        with self.assertRaises(ValueError):
            parse_program(["ADD R1,R0,R0"] * 25)
        evs = parse_events(["dispatch 0"] * 96)
        self.assertEqual(len(evs), 96)
        with self.assertRaises(ValueError):
            parse_events(["dispatch 0"] * 97)


class DispatchRenameTests(unittest.TestCase):
    def test_initial_mapping_and_allocation(self):
        report = run("ADD R1,R0,R0", "dispatch I0")
        step = report["steps"][0]
        self.assertEqual(step["before"]["map_table"], list(range(ARCH_REGS)))
        self.assertEqual(step["after"]["map_table"][1], 8)
        self.assertEqual(step["action"]["old_tag"], 1)
        self.assertEqual(step["action"]["src_tags"], [0, 0])
        self.assertNotIn(8, step["after"]["free_list"])

    def test_branch_dispatch_checkpoint_no_tag(self):
        events = "dispatch I0\ndispatch I1"
        report = run("ADD R1,R0,R0\nBEQ R1,R0 predict=taken", events)
        br = report["steps"][1]
        self.assertIsNone(br["action"]["tag"])
        self.assertIn("1", br["after"]["checkpoints"])
        # only one free tag (P8) consumed by I0; P9 still free
        self.assertEqual(br["after"]["free_list"], list(range(9, 16)))
        cp = br["after"]["checkpoints"]["1"]
        self.assertEqual(cp["map_table"][1], 8)

    def test_dispatch_source_uses_current_mapping(self):
        prog = "ADD R1,R0,R0\nADD R2,R1,R0"
        report = run(prog, "dispatch I0\ndispatch I1")
        self.assertEqual(report["steps"][1]["action"]["src_tags"], [8, 0])

    def test_dispatch_must_be_in_order(self):
        code, _ = violation_code("ADD R1,R0,R0\nADD R2,R0,R0",
                                 "dispatch I1")
        self.assertEqual(code, "DISPATCH_OUT_OF_ORDER")

    def test_duplicate_dispatch_rejected(self):
        code, _ = violation_code("ADD R1,R0,R0", "dispatch I0\ndispatch I0")
        self.assertEqual(code, "DISPATCH_DUPLICATE")

    def test_free_list_exhaustion(self):
        prog = "\n".join(f"ADD R{r % 7 + 1},R0,R0" for r in range(8))
        events = "\n".join(f"dispatch I{i}" for i in range(8))
        code, report = violation_code(prog, events, num_phys=15)
        self.assertEqual(code, "NO_FREE_PHYS_REG")
        self.assertEqual(report["violation_step"], 7)


class WritebackCommitTests(unittest.TestCase):
    def test_commit_unfinished_head(self):
        code, report = violation_code(
            "ADD R1,R0,R0\nADD R2,R1,R0\nADD R3,R2,R1",
            "dispatch I0\ndispatch I1\nwriteback I1\ncommit I1")
        self.assertEqual(code, "COMMIT_NOT_HEAD")

    def test_commit_head_not_done(self):
        code, _ = violation_code(
            "ADD R1,R0,R0\nADD R2,R1,R0",
            "dispatch I0\ndispatch I1\ncommit I0")
        self.assertEqual(code, "COMMIT_UNFINISHED")

    def test_commit_unresolved_branch(self):
        code, _ = violation_code(
            "ADD R1,R0,R0\nBEQ R1,R0 predict=taken",
            "dispatch I0\ndispatch I1\nwriteback I0\nwriteback I1\ncommit I0\ncommit I1")
        self.assertEqual(code, "COMMIT_UNRESOLVED_BRANCH")

    def test_commit_frees_old_tag(self):
        report = run(
            "ADD R1,R0,R0",
            "dispatch I0\nwriteback I0\ncommit I0")
        commit = report["steps"][2]
        self.assertEqual(commit["action"]["reclaimed_old_tag"], 1)
        self.assertIn(1, commit["after"]["free_list"])
        self.assertEqual(commit["after"]["map_table"][1], 8)  # committed mapping stays
        self.assertEqual(commit["after"]["committed"], [0])

    def test_writeback_requires_dispatch(self):
        code, _ = violation_code("ADD R1,R0,R0", "writeback I0")
        self.assertEqual(code, "WRITEBACK_NOT_DISPATCHED")

    def test_duplicate_writeback(self):
        code, _ = violation_code("ADD R1,R0,R0",
                                 "dispatch I0\nwriteback I0\nwriteback I0")
        self.assertEqual(code, "WRITEBACK_DUPLICATE")

    def test_commit_empty_rob(self):
        code, _ = violation_code("ADD R1,R0,R0", "commit")
        self.assertEqual(code, "COMMIT_EMPTY_ROB")


class CorrectBranchFlowTests(unittest.TestCase):
    def test_correct_prediction_retire_path(self):
        report = run(
            "ADD R1,R0,R0\nBNE R1,R0 predict=not-taken\nADD R2,R1,R0",
            """
            dispatch I0
            dispatch I1
            dispatch I2
            writeback I0
            writeback I1
            resolve I1 not-taken
            commit I0
            commit I1
            writeback I2
            commit I2
            """)
        self.assertTrue(report["ok"], report["violation"])
        self.assertEqual(report["final"]["committed"], [0, 1, 2])
        # checkpoint retired on correct prediction
        self.assertEqual(report["final"]["checkpoints"], {})

    def test_full_misprediction_then_refetch_commits(self):
        events = """
        dispatch I0
        dispatch I1
        dispatch I2
        dispatch I3
        dispatch I4
        writeback I0
        writeback I1
        commit I0
        writeback I2
        resolve I2 not-taken
        dispatch I5
        dispatch I6
        writeback I5
        writeback I6
        commit I1
        commit I2
        commit I5
        commit I6
        """
        report = run(PROG_BRANCH, events)
        self.assertTrue(report["ok"], report["violation"])
        final = report["final"]
        self.assertEqual(final["committed"], [0, 1, 2, 5, 6])
        self.assertEqual(set(final["squashed"]), {3, 4})
        # wrong-path younger results never present in committed sequence
        self.assertFalse({3, 4} & set(final["committed"]))


class BranchRollbackTests(unittest.TestCase):
    def setUp(self):
        self.events_to_resolve = """
        dispatch I0
        dispatch I1
        dispatch I2
        dispatch I3
        dispatch I4
        writeback I0
        writeback I1
        commit I0
        writeback I2
        resolve I2 not-taken
        """

    def test_mispredict_squashes_younger_and_restores_checkpoint(self):
        report = run(PROG_BRANCH, self.events_to_resolve)
        self.assertTrue(report["ok"], report["violation"])
        step = report["steps"][-1]
        action = step["action"]
        self.assertTrue(action["mispredicted"])
        seqs = [s["seq"] for s in action["squashed"]]
        self.assertEqual(seqs, [3, 4])
        # tags P11 (I3->R3), P12 (I4->R4) reclaimed into the free list
        self.assertIn(11, step["after"]["free_list"])
        self.assertIn(12, step["after"]["free_list"])
        # ROB keeps only I1 and branch I2
        self.assertEqual([e["seq"] for e in step["after"]["rob"]], [1, 2])
        # map restored: R3 -> P3, R4 -> P4 (architectural defaults)
        self.assertEqual(step["after"]["map_table"][3], 3)
        self.assertEqual(step["after"]["map_table"][4], 4)
        # older rename survives: R1 -> P8 committed, R2 -> P9 in flight
        self.assertEqual(step["after"]["map_table"][1], 8)
        self.assertEqual(step["after"]["map_table"][2], 9)
        # checkpoint consumed
        self.assertNotIn("2", step["after"]["checkpoints"])

    def test_mispredict_rejects_late_writeback_of_squashed(self):
        events = self.events_to_resolve + "\nwriteback I3"
        code, report = violation_code(PROG_BRANCH, events)
        self.assertEqual(code, "WRITEBACK_AFTER_SQUASH")
        self.assertEqual(report["violation_step"], 10)
        evidence = report["violation"]["evidence"]
        self.assertIn(3, evidence["squashed"])

    def test_resolve_non_branch(self):
        code, _ = violation_code("ADD R1,R0,R0",
                                 "dispatch I0\nwriteback I0\nresolve I0 taken")
        self.assertEqual(code, "RESOLVE_NOT_BRANCH")

    def test_resolve_before_writeback(self):
        code, _ = violation_code("BEQ R0,R0 predict=taken",
                                 "dispatch I0\nresolve I0 not-taken")
        self.assertEqual(code, "RESOLVE_UNFINISHED")

    def test_resolve_unknown_seq(self):
        code, _ = violation_code("BEQ R0,R0 predict=taken", "resolve I9 taken")
        self.assertEqual(code, "RESOLVE_UNKNOWN_SEQ")

    def test_wrong_path_writes_cannot_commit(self):
        # After squash I3/I4, a commit event naming I1 is legal only once
        # done; attempt to commit squashed I3 must fail as not in ROB head.
        events = self.events_to_resolve + "\ncommit I3"
        code, _ = violation_code(PROG_BRANCH, events)
        self.assertEqual(code, "COMMIT_NOT_HEAD")

    def test_dispatch_after_rollback_reuses_freed_tag(self):
        events = self.events_to_resolve + "\ndispatch I5"
        report = run(PROG_BRANCH, events)
        self.assertTrue(report["ok"], report["violation"])
        # I5 writes R1; the lowest free tag after restore includes P10/P11...
        dispatch = report["steps"][-1]["action"]
        free_after_rollback = sorted(
            report["steps"][-2]["after"]["free_list"])
        self.assertEqual(dispatch["tag"], free_after_rollback[0])

    def test_nested_branch_inner_mispredict_keeps_outer_checkpoint(self):
        prog = """
        ADD R1,R0,R0
        BEQ R1,R0 predict=not-taken
        ADD R2,R1,R0
        BNE R2,R1 predict=taken
        ADD R3,R2,R1
        """
        events = """
        dispatch I0
        dispatch I1
        dispatch I2
        dispatch I3
        dispatch I4
        writeback I0
        writeback I1
        writeback I2
        writeback I3
        resolve I3 not-taken
        """
        report = run(prog, events)
        self.assertTrue(report["ok"], report["violation"])
        after = report["steps"][-1]["after"]
        # inner branch squashes only I4; outer branch I1 stays live with cp
        self.assertEqual(after["squashed"], [4])
        self.assertIn("1", after["checkpoints"])
        self.assertNotIn("3", after["checkpoints"])
        # outer checkpoint predates I2's rename: R2 -> P2 (arch default)
        self.assertEqual(after["checkpoints"]["1"]["map_table"][2], 2)
        # current map keeps I2's in-flight rename: R2 -> P9
        self.assertEqual(after["map_table"][2], 9)
        # I4's tag P10 is restored to free list; P9 stays allocated
        self.assertIn(10, after["free_list"])
        self.assertNotIn(9, after["free_list"])
        self.assertEqual([e["seq"] for e in after["rob"]], [0, 1, 2, 3])

    def test_resolved_branch_head_can_commit_after_rollback(self):
        events = self.events_to_resolve + """
        commit I1
        commit I2
        """
        report = run(PROG_BRANCH, events)
        self.assertTrue(report["ok"], report["violation"])
        self.assertEqual(report["final"]["committed"], [0, 1, 2])


class ExceptionBoundaryTests(unittest.TestCase):
    def setUp(self):
        # I0 commits, then I2 faults while head is I1.
        self.events = """
        dispatch I0
        dispatch I1
        dispatch I2
        dispatch I3
        dispatch I4
        writeback I0
        commit I0
        writeback I1
        writeback I2
        exception I2
        writeback I3
        commit I1
        """

    def test_exception_drains_at_head_without_commit(self):
        report = run(PROG_EXC, self.events)
        self.assertTrue(report["ok"], report["violation"])
        commit_step = report["steps"][-1]
        rb = commit_step["action"]["exception_rollback"]
        self.assertEqual(rb["fault_seq"], 2)
        disc = {d["seq"]: d for d in rb["discarded"]}
        self.assertTrue(disc[2]["is_faulting"])
        self.assertEqual(set(disc), {2, 3, 4})
        # faulting target R3 and younger R4/R5 results are discarded
        self.assertEqual(disc[2]["tag"], 10)
        # precise mapping restored, committed I0 (R1->P8) survives
        self.assertEqual(rb["precise_map"][3], 3)
        self.assertEqual(rb["precise_map"][4], 4)
        self.assertEqual(rb["precise_map"][5], 5)
        self.assertEqual(rb["precise_map"][1], 8)
        final = report["final"]
        self.assertEqual(final["committed"], [0, 1])
        self.assertEqual(final["rob"], [])
        # all discarded tags back on the free list plus I1's old tag P2
        self.assertIn(10, final["free_list"])
        self.assertIn(11, final["free_list"])
        self.assertIn(12, final["free_list"])
        self.assertIn(2, final["free_list"])

    def test_exception_immediately_at_head(self):
        events = """
        dispatch I0
        dispatch I1
        writeback I0
        writeback I1
        exception I0
        """
        report = run(PROG_EXC, events)
        self.assertTrue(report["ok"], report["violation"])
        rb = report["steps"][-1]["action"]["rollback"]
        self.assertEqual(set(d["seq"] for d in rb["discarded"]), {0, 1})
        self.assertEqual(rb["precise_map"], list(range(ARCH_REGS)))
        self.assertEqual(report["final"]["committed"], [])

    def test_late_writeback_after_exception_squash_rejected(self):
        # fault I1 at head (I0 committed first): I2 squashed immediately
        events = """
        dispatch I0
        dispatch I1
        dispatch I2
        writeback I0
        commit I0
        writeback I1
        exception I1
        writeback I2
        """
        code, report = violation_code(PROG_EXC, events)
        self.assertEqual(code, "WRITEBACK_AFTER_SQUASH")
        self.assertEqual(report["violation_step"], 7)

    def test_exception_on_committed_instruction_impossible(self):
        events = """
        dispatch I0
        writeback I0
        commit I0
        exception I0
        """
        code, _ = violation_code(PROG_EXC, events)
        self.assertEqual(code, "EXCEPTION_AFTER_COMMIT")

    def test_duplicate_exception(self):
        code, _ = violation_code(
            PROG_EXC,
            "dispatch I0\ndispatch I1\nwriteback I0\nwriteback I1\nexception I1\nexception I1")
        self.assertEqual(code, "EXCEPTION_DUPLICATE")

    def test_exception_requires_dispatch(self):
        code, _ = violation_code(PROG_EXC, "exception I3")
        self.assertEqual(code, "EXCEPTION_NOT_DISPATCHED")

    def test_precise_mapping_youngest_first_with_same_dest(self):
        # I2 and I4 both write R3; walking youngest-first must restore R3 to
        # I2's old tag (P3), not I4's (which is I2's tag).
        prog = """
        ADD R1,R0,R0
        ADD R2,R1,R0
        MUL R3,R1,R2
        ADD R4,R3,R1
        ADD R3,R4,R2
        """
        events = """
        dispatch I0
        dispatch I1
        dispatch I2
        dispatch I3
        dispatch I4
        writeback I0
        commit I0
        exception I1
        """
        report = run(prog, events)
        self.assertTrue(report["ok"], report["violation"])
        rb = report["steps"][-1]["action"]["rollback"]
        self.assertEqual(rb["precise_map"][3], 3)
        self.assertEqual(rb["precise_map"][4], 4)
        restored = {(m["seq"], m["arch"]): (m["from"], m["to"])
                    for m in rb["restored_mappings"]}
        self.assertEqual(restored[(4, 3)], (12, 10))  # I4 tag -> I2 tag
        self.assertEqual(restored[(2, 3)], (10, 3))   # I2 tag -> arch P3


class IncrementalSimulatorTests(unittest.TestCase):
    def test_step_then_continue(self):
        instrs = parse_program("ADD R1,R0,R0\nADD R2,R1,R0")
        evs = parse_events("dispatch I0\ndispatch I1")
        sim = Simulator(instrs)
        s0 = sim.step(evs[0])
        self.assertEqual(s0["after"]["map_table"][1], 8)
        s1 = sim.step(evs[1])
        self.assertEqual(s1["after"]["map_table"][2], 9)
        self.assertIsNone(s1["violation"])

    def test_step_marks_violation_and_stops(self):
        instrs = parse_program("ADD R1,R0,R0")
        evs = parse_events("dispatch I0\ncommit I0")
        sim = Simulator(instrs)
        sim.step(evs[0])
        s1 = sim.step(evs[1])
        self.assertEqual(s1["violation"]["code"], "COMMIT_UNFINISHED")
        self.assertTrue(sim.finished)


if __name__ == "__main__":
    unittest.main(verbosity=2)
