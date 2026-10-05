"""
Out-of-order execution trace reviewer — core simulator.

Models a checkpoint-based rename / ROB machine:

* 8 architectural registers R0..R7, initially mapped to physical registers
  P0..P7; additional physical registers are allocatable from a free list.
* in-order dispatch renames the destination, records the previous mapping
  (old tag) in the ROB, and snapshots a per-conditional-branch checkpoint.
  Every dispatch of a static instruction creates a new execution instance
  (generation); a re-fetch after a squash starts the next generation.
* writeback completes the oldest execution instance of the static
  instruction that has not yet received a completion. A writeback that
  belongs to an already-squashed instance — including a leftover writeback
  from a first execution that arrives after the instruction was re-fetched —
  is a violation: the re-fetched execution may only complete from its own
  writeback.
* resolve on a conditional branch: on a misprediction every younger ROB
  entry is squashed and the map / free list are restored from that branch's
  checkpoint; a correctly predicted branch simply retires its checkpoint.
* commit only services the completed, resolved head of the ROB and frees the
  old physical tag.
* exception marks an ROB entry; once it reaches the head it never commits —
  it and every younger result are discarded and the precise pre-exception
  mapping is reconstructed by walking old-tags youngest-first.

The replay stops at the first violating event and keeps before/after
evidence (map table, ROB, free list, checkpoints, reclaimed tags).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional

ARCH_REGS = 8
DEFAULT_NUM_PHYS = 16
MAX_INSTRUCTIONS = 24
MAX_EVENTS = 96

BRANCH_OPS = {"BEQ", "BNE"}
ALU_OPS = {"ADD", "SUB", "AND", "OR", "XOR", "SLT", "MUL"}


class Violation(Exception):
    """A trace event contradicts legal out-of-order machine behavior."""

    def __init__(self, code: str, message: str, evidence: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.evidence = evidence or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "evidence": self.evidence}


@dataclass
class Instruction:
    seq: int
    text: str
    op: str
    dest: Optional[int]
    srcs: list  # list[int]
    is_branch: bool
    predicted_taken: bool = False

    @property
    def name(self) -> str:
        if self.is_branch:
            pred = "taken" if self.predicted_taken else "not-taken"
            s = ",".join(f"R{r}" for r in self.srcs)
            return f"{self.op} {s} predict={pred}"
        s = ",".join(f"R{r}" for r in self.srcs)
        return f"{self.op} R{self.dest},{s}"


@dataclass
class Event:
    kind: str  # dispatch | writeback | resolve | commit | exception
    seq: Optional[int] = None
    taken: Optional[bool] = None
    text: str = ""

    def describe(self) -> str:
        if self.kind == "dispatch":
            return f"dispatch I{self.seq}"
        if self.kind == "writeback":
            return f"writeback I{self.seq}"
        if self.kind == "resolve":
            direction = "taken" if self.taken else "not-taken"
            return f"resolve I{self.seq} {direction}"
        if self.kind == "exception":
            return f"exception I{self.seq}"
        return f"commit I{self.seq}" if self.seq is not None else "commit (head)"


@dataclass
class RobEntry:
    seq: int
    generation: int
    instr: Instruction
    tag: int
    old_tag: Optional[int]
    done: bool = False
    resolved: bool = False
    taken: Optional[bool] = None
    has_exception: bool = False

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "generation": self.generation,
            "name": self.instr.name,
            "op": self.instr.op,
            "dest": self.instr.dest,
            "srcs": list(self.instr.srcs),
            "is_branch": self.instr.is_branch,
            "tag": self.tag,
            "old_tag": self.old_tag,
            "done": self.done,
            "resolved": self.resolved,
            "taken": self.taken,
            "has_exception": self.has_exception,
        }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_register(token: str) -> int:
    t = token.strip().upper()
    if not (t.startswith("R") and t[1:].isdigit()):
        raise ValueError(f"bad register '{token}', expected R0..R7")
    r = int(t[1:])
    if not 0 <= r < ARCH_REGS:
        raise ValueError(f"register R{r} out of range (R0..R7)")
    return r


def parse_program(lines) -> list:
    """Parse program lines into Instructions. Raises ValueError on error."""
    if isinstance(lines, str):
        lines = lines.splitlines()
    instructions = []
    for idx, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        body = line
        if ":" in line:
            prefix, body = line.split(":", 1)
            p = prefix.strip().upper()
            if not (p.startswith("I") and p[1:].isdigit()):
                raise ValueError(f"line {idx + 1}: bad label '{prefix.strip()}'")
            if int(p[1:]) != len(instructions):
                raise ValueError(
                    f"line {idx + 1}: label {p} out of order, expected I{len(instructions)}"
                )
        toks = body.replace(",", " ").split()
        if not toks:
            continue
        op = toks[0].strip().upper()
        operands = toks[1:]
        predicted_taken = False
        predict_tok = None
        cleaned = []
        for o in operands:
            if o.lower().startswith("predict="):
                predict_tok = o.split("=", 1)[1].strip().lower()
            else:
                cleaned.append(o)
        if predict_tok not in (None, "taken", "not-taken", "not_taken", "fallthrough"):
            raise ValueError(f"line {idx + 1}: bad prediction '{predict_tok}'")
        if predict_tok == "taken":
            predicted_taken = True

        if op in BRANCH_OPS:
            if len(cleaned) != 2:
                raise ValueError(f"line {idx + 1}: {op} expects two source registers")
            srcs = [_parse_register(c) for c in cleaned]
            instr = Instruction(
                seq=len(instructions),
                text=line,
                op=op,
                dest=None,
                srcs=srcs,
                is_branch=True,
                predicted_taken=predicted_taken,
            )
        elif op in ALU_OPS:
            if len(cleaned) != 3:
                raise ValueError(f"line {idx + 1}: {op} expects Rd, Rs, Rt")
            dest = _parse_register(cleaned[0])
            srcs = [_parse_register(c) for c in cleaned[1:]]
            instr = Instruction(
                seq=len(instructions),
                text=line,
                op=op,
                dest=dest,
                srcs=srcs,
                is_branch=False,
            )
        else:
            raise ValueError(f"line {idx + 1}: unknown opcode '{op}'")
        instructions.append(instr)

    if not instructions:
        raise ValueError("program is empty")
    if len(instructions) > MAX_INSTRUCTIONS:
        raise ValueError(f"at most {MAX_INSTRUCTIONS} instructions are allowed")
    return instructions


def parse_events(lines) -> list:
    """Parse event lines into Events. Raises ValueError on error."""
    if isinstance(lines, str):
        lines = lines.splitlines()
    events = []
    for idx, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        toks = line.replace(",", " ").split()
        kind = toks[0].strip().lower()
        args = toks[1:]
        try:
            if kind == "dispatch":
                seq = _seq_arg(args, 0, idx)
                events.append(Event("dispatch", seq=seq, text=line))
            elif kind == "writeback" or kind == "wb":
                seq = _seq_arg(args, 0, idx)
                events.append(Event("writeback", seq=seq, text=line))
            elif kind == "resolve":
                seq = _seq_arg(args, 0, idx)
                if len(args) < 2:
                    raise ValueError("resolve needs <seq> taken|not-taken")
                direction = args[1].strip().lower()
                if direction in ("taken", "t", "1"):
                    taken = True
                elif direction in ("not-taken", "not_taken", "n", "nt", "0", "fallthrough"):
                    taken = False
                else:
                    raise ValueError(f"bad direction '{args[1]}'")
                events.append(Event("resolve", seq=seq, taken=taken, text=line))
            elif kind == "commit":
                seq = _seq_arg(args, 0, idx, required=False) if args else None
                events.append(Event("commit", seq=seq, text=line))
            elif kind == "exception" or kind == "exc":
                seq = _seq_arg(args, 0, idx)
                events.append(Event("exception", seq=seq, text=line))
            else:
                raise ValueError(f"unknown event kind '{kind}'")
        except ValueError as exc:
            raise ValueError(f"line {idx + 1}: {exc}") from None

    if len(events) > MAX_EVENTS:
        raise ValueError(f"at most {MAX_EVENTS} events are allowed")
    return events


def _seq_arg(args, pos, line_idx, required=True):
    if len(args) <= pos:
        if required:
            raise ValueError("missing instruction sequence number")
        return None
    t = args[pos].strip().upper()
    if t.startswith("I") and t[1:].isdigit():
        t = t[1:]
    if not t.isdigit():
        raise ValueError(f"bad sequence number '{args[pos]}'")
    return int(t)


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

class Simulator:
    def __init__(self, instructions: list, num_phys: int = DEFAULT_NUM_PHYS):
        if num_phys < ARCH_REGS:
            raise ValueError(f"need at least {ARCH_REGS} physical registers")
        self.instructions = instructions
        self.num_phys = num_phys
        self.map_table = list(range(ARCH_REGS))          # arch reg -> phys tag
        self.free_list = list(range(ARCH_REGS, num_phys))
        self.rob = []                                    # list[RobEntry], head first
        self.by_seq: dict[int, RobEntry] = {}
        self.dispatch_generations: dict[int, int] = {}
        self.checkpoints: dict[int, dict] = {}           # branch seq -> checkpoint
        self.next_dispatch = 0
        self.committed = []
        self.squashed = set()                # seqs with at least one squashed instance
        # Per static instruction, every dispatch spawns one execution instance
        # (generation). Writebacks are matched to instances oldest-first, so a
        # leftover writeback of a squashed generation can never complete the
        # re-fetched generation.
        self.instances: dict[int, list[dict]] = {}       # seq -> [instance]
        self.squash_log = []                 # per-instance squash evidence
        self.counters = {
            "dispatch": 0,
            "writeback": 0,
            "resolve": 0,
            "mispredict": 0,
            "commit": 0,
            "exception": 0,
        }
        self.finished = False
        self.violation: Optional[dict] = None
        self.processed = 0
        self.rollback_log = []   # precise-exception rollbacks performed

    # -- snapshots ----------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "map_table": list(self.map_table),
            "free_list": sorted(self.free_list),
            "rob": [e.to_dict() for e in self.rob],
            "committed": list(self.committed),
            "squashed": sorted(self.squashed),
            "squashed_instances": [dict(rec) for rec in self.squash_log],
            "checkpoints": {
                str(seq): {
                    "map_table": list(cp["map_table"]),
                    "free_list": sorted(cp["free_list"]),
                }
                for seq, cp in self.checkpoints.items()
            },
            "next_dispatch": self.next_dispatch,
            "dispatch_generations": {
                str(seq): generation
                for seq, generation in self.dispatch_generations.items()
            },
            "counters": dict(self.counters),
        }

    def _lookup(self, seq) -> Optional[RobEntry]:
        entry = self.by_seq.get(seq)
        if entry is not None and entry in self.rob:
            return entry
        return None

    def _instance_of(self, entry: RobEntry) -> Optional[dict]:
        for inst in self.instances.get(entry.seq, []):
            if inst["generation"] == entry.generation:
                return inst
        return None

    def _record_squash(self, entry: RobEntry, cause: str, by_seq: int,
                       is_faulting: bool = False) -> dict:
        """Mark the entry's execution instance squashed and keep the
        per-instance reclaim evidence (tag / old tag / dest / completion)."""
        inst = self._instance_of(entry)
        if inst is not None:
            inst["status"] = "squashed"
        self.squashed.add(entry.seq)
        record = {
            "seq": entry.seq,
            "generation": entry.generation,
            "tag": entry.tag,
            "old_tag": entry.old_tag,
            "dest": entry.instr.dest,
            "was_done": entry.done,
            "cause": cause,
            "by_seq": by_seq,
            "is_faulting": is_faulting,
        }
        self.squash_log.append(record)
        return record

    # -- event application --------------------------------------------------

    def apply(self, ev: Event) -> dict:
        """Apply one event; returns a small action record. Raises Violation."""
        if ev.kind == "dispatch":
            return self._do_dispatch(ev)
        if ev.kind == "writeback":
            return self._do_writeback(ev)
        if ev.kind == "resolve":
            return self._do_resolve(ev)
        if ev.kind == "commit":
            return self._do_commit(ev)
        if ev.kind == "exception":
            return self._do_exception(ev)
        raise Violation("UNKNOWN_EVENT", f"unknown event kind {ev.kind}")

    def _do_dispatch(self, ev: Event) -> dict:
        seq = ev.seq
        if seq is None or not 0 <= seq < len(self.instructions):
            raise Violation(
                "DISPATCH_UNKNOWN_SEQ",
                f"dispatch refers to I{seq}, program only has {len(self.instructions)} instructions",
            )
        if self._lookup(seq) is not None or seq in self.committed:
            raise Violation("DISPATCH_DUPLICATE", f"I{seq} was already dispatched")
        refetching = seq in self.squashed
        if not refetching and seq != self.next_dispatch:
            raise Violation(
                "DISPATCH_OUT_OF_ORDER",
                f"dispatch is in program order: expected I{self.next_dispatch}, got I{seq}",
            )
        if not self.free_list:
            raise Violation(
                "NO_FREE_PHYS_REG",
                f"cannot dispatch I{seq}: free list exhausted "
                f"({self.num_phys} physical registers, {len(self.rob)} in ROB)",
                {"rob_size": len(self.rob)},
            )

        instr = self.instructions[seq]
        generation = self.dispatch_generations.get(seq, 0) + 1
        # A re-fetched instruction keeps its earlier squashed generations on
        # record: a leftover writeback of such a generation must still be
        # rejected instead of completing this new execution.
        squashed_generations = [
            inst["generation"] for inst in self.instances.get(seq, [])
            if inst["status"] == "squashed"
        ]

        # Only instructions that write an architectural register rename a
        # destination tag; branches consume no free-list entry.
        tag = self.free_list.pop(0) if instr.dest is not None else None
        old_tag = self.map_table[instr.dest] if instr.dest is not None else None

        # Source operands read through the current rename mapping.
        src_tags = [self.map_table[s] for s in instr.srcs]

        checkpoint = None
        if instr.is_branch:
            # Checkpoint rename state immediately after this branch occupies
            # its ROB slot, so a mispredict restores exactly what the
            # re-fetched path must see.
            checkpoint = {
                "map_table": list(self.map_table),
                "free_list": sorted(self.free_list),
            }
            self.checkpoints[seq] = checkpoint

        entry = RobEntry(
            seq=seq, generation=generation, instr=instr, tag=tag, old_tag=old_tag
        )
        self.rob.append(entry)
        self.by_seq[seq] = entry
        self.dispatch_generations[seq] = generation
        self.instances.setdefault(seq, []).append({
            "generation": generation,
            "status": "inflight",
            "wb_matched": False,
            "tag": tag,
            "old_tag": old_tag,
            "dest": instr.dest,
        })
        if instr.dest is not None:
            self.map_table[instr.dest] = tag
        if not refetching:
            self.next_dispatch += 1
        self.counters["dispatch"] += 1
        return {
            "action": "dispatch",
            "seq": seq,
            "generation": generation,
            "refetched": refetching,
            "squashed_generations": squashed_generations,
            "tag": tag,
            "old_tag": old_tag,
            "src_tags": src_tags,
            "checkpoint": deepcopy(checkpoint) if checkpoint else None,
        }

    def _do_writeback(self, ev: Event) -> dict:
        seq = ev.seq
        if seq is None or not 0 <= seq < len(self.instructions):
            raise Violation("WRITEBACK_UNKNOWN_SEQ", f"writeback refers to unknown I{seq}")
        insts = self.instances.get(seq, [])
        # A writeback completes the oldest execution instance of this static
        # instruction that has not yet received its completion event.
        pending = next((inst for inst in insts if not inst["wb_matched"]), None)
        if pending is None:
            if not insts:
                raise Violation(
                    "WRITEBACK_NOT_DISPATCHED",
                    f"writeback for I{seq} before it was dispatched",
                )
            if seq in self.committed:
                raise Violation("WRITEBACK_AFTER_COMMIT", f"I{seq} already committed")
            if any(inst["status"] == "squashed" for inst in insts):
                raise Violation(
                    "WRITEBACK_AFTER_SQUASH",
                    f"writeback for I{seq} rejected: its execution was already "
                    f"squashed by an older resolved branch or precise-exception "
                    f"rollback; its result must never reach committed "
                    f"architectural state",
                    self._squash_evidence(seq),
                )
            raise Violation("WRITEBACK_DUPLICATE", f"I{seq} already wrote back")
        if pending["status"] == "squashed":
            current = self.dispatch_generations.get(seq)
            raise Violation(
                "WRITEBACK_AFTER_SQUASH",
                f"writeback for I{seq} rejected: it belongs to execution "
                f"#{pending['generation']}, which was already squashed by an older "
                f"resolved branch or precise-exception rollback; its result must "
                f"never reach committed architectural state"
                + (
                    f" — the re-fetched execution #{current} may only complete "
                    f"from its own writeback"
                    if current != pending["generation"] else ""
                ),
                self._squash_evidence(seq),
            )
        entry = self._lookup(seq)
        if entry is None or entry.generation != pending["generation"]:
            raise Violation(  # defensive: an in-flight instance is always in the ROB
                "WRITEBACK_NOT_DISPATCHED",
                f"writeback for I{seq} before it was dispatched",
            )
        pending["wb_matched"] = True
        entry.done = True
        self.counters["writeback"] += 1
        return {
            "action": "writeback",
            "seq": seq,
            "generation": entry.generation,
            "tag": entry.tag,
        }

    def _do_resolve(self, ev: Event) -> dict:
        seq = ev.seq
        if seq is None or not 0 <= seq < len(self.instructions):
            raise Violation("RESOLVE_UNKNOWN_SEQ", f"resolve refers to unknown I{seq}")
        entry = self._lookup(seq)
        if entry is None:
            where = "squashed" if seq in self.squashed else "committed" if seq in self.committed else "not dispatched"
            raise Violation(
                "RESOLVE_STALE",
                f"resolve for I{seq} but the branch is {where}",
                self._squash_evidence(seq),
            )
        if not entry.instr.is_branch:
            raise Violation("RESOLVE_NOT_BRANCH", f"I{seq} ({entry.instr.op}) is not a conditional branch")
        if entry.resolved:
            raise Violation("RESOLVE_DUPLICATE", f"branch I{seq} already resolved")
        if not entry.done:
            raise Violation(
                "RESOLVE_UNFINISHED",
                f"branch I{seq} resolved before its writeback completed",
            )

        entry.resolved = True
        entry.taken = ev.taken
        self.counters["resolve"] += 1
        mispredicted = bool(ev.taken) != entry.instr.predicted_taken

        if not mispredicted:
            self.checkpoints.pop(seq, None)
            return {
                "action": "resolve",
                "seq": seq,
                "taken": ev.taken,
                "mispredicted": False,
            }

        # Misprediction: squash every younger instruction and restore the
        # checkpoint captured at the branch's dispatch.
        self.counters["mispredict"] += 1
        cp = self.checkpoints.get(seq)
        if cp is None:  # defensive: checkpoint must exist for a live branch
            raise Violation("MISSING_CHECKPOINT", f"no checkpoint for mispredicted branch I{seq}")

        younger = [e for e in self.rob if e.seq > seq]
        squashed_records = []
        for e in younger:
            squashed_records.append(self._record_squash(e, "mispredict", seq))
            self.rob.remove(e)
            self.by_seq.pop(e.seq, None)
            self.checkpoints.pop(e.seq, None)

        restored = [
            {"arch": r, "from": self.map_table[r], "to": cp["map_table"][r]}
            for r in range(ARCH_REGS)
            if self.map_table[r] != cp["map_table"][r]
        ]
        reclaimed = sorted(e["tag"] for e in squashed_records
                           if e["tag"] is not None)
        before_map, before_free = list(self.map_table), sorted(self.free_list)
        self.map_table = list(cp["map_table"])
        self.free_list = sorted(cp["free_list"])
        self.checkpoints.pop(seq, None)

        return {
            "action": "resolve",
            "seq": seq,
            "taken": ev.taken,
            "mispredicted": True,
            "predicted_taken": entry.instr.predicted_taken,
            "squashed": squashed_records,
            "reclaimed_tags": reclaimed,
            "restored_mappings": restored,
            "checkpoint": {"map_table": list(cp["map_table"]), "free_list": sorted(cp["free_list"])},
            "before_rollback": {"map_table": before_map, "free_list": before_free},
        }

    def _do_commit(self, ev: Event) -> dict:
        if not self.rob:
            raise Violation(
                "COMMIT_EMPTY_ROB",
                "commit with an empty ROB: nothing is in flight",
            )
        head = self.rob[0]
        if ev.seq is not None and ev.seq != head.seq:
            raise Violation(
                "COMMIT_NOT_HEAD",
                f"commit targets I{ev.seq} but ROB head is I{head.seq}; "
                f"commit is strictly in program order",
                {"head_seq": head.seq, "requested_seq": ev.seq},
            )
        if head.has_exception:
            # Defensive: the head exception is normally drained automatically.
            raise Violation(
                "COMMIT_EXCEPTION_AT_HEAD",
                f"exception-carrying I{head.seq} is at ROB head and must never commit",
                self._exception_evidence(head),
            )
        if not head.done:
            raise Violation(
                "COMMIT_UNFINISHED",
                f"ROB head I{head.seq} has not written back; it cannot commit",
                {"head": head.to_dict()},
            )
        if head.instr.is_branch and not head.resolved:
            raise Violation(
                "COMMIT_UNRESOLVED_BRANCH",
                f"ROB head I{head.seq} is a branch not yet resolved; "
                f"committing it could retire a wrong-path prediction",
                {"head": head.to_dict()},
            )

        self.rob.pop(0)
        self.by_seq.pop(head.seq, None)
        self.committed.append(head.seq)
        inst = self._instance_of(head)
        if inst is not None:
            inst["status"] = "committed"
        self.counters["commit"] += 1
        reclaimed_old = None
        if head.old_tag is not None:
            self.free_list.append(head.old_tag)
            self.free_list.sort()
            reclaimed_old = head.old_tag
        self.checkpoints.pop(head.seq, None)

        rollback = self._drain_exception_head()
        return {
            "action": "commit",
            "seq": head.seq,
            "reclaimed_old_tag": reclaimed_old,
            "exception_rollback": rollback,
        }

    def _do_exception(self, ev: Event) -> dict:
        seq = ev.seq
        if seq is None or not 0 <= seq < len(self.instructions):
            raise Violation("EXCEPTION_UNKNOWN_SEQ", f"exception refers to unknown I{seq}")
        entry = self._lookup(seq)
        if entry is None:
            if seq in self.committed:
                raise Violation(
                    "EXCEPTION_AFTER_COMMIT",
                    f"exception for I{seq} arrives after commit: architectural state already "
                    f"contains the faulting result — precise exception is impossible",
                    {"committed": list(self.committed)},
                )
            if seq in self.squashed:
                raise Violation(
                    "EXCEPTION_AFTER_SQUASH",
                    f"exception for I{seq} but the instruction was already squashed",
                    self._squash_evidence(seq),
                )
            raise Violation(
                "EXCEPTION_NOT_DISPATCHED",
                f"exception for I{seq} before it was dispatched",
            )
        if entry.has_exception:
            raise Violation("EXCEPTION_DUPLICATE", f"I{seq} already carries an exception")

        entry.has_exception = True
        self.counters["exception"] += 1
        rollback = self._drain_exception_head()
        return {"action": "exception", "seq": seq, "rollback": rollback}

    # -- precise exception support -----------------------------------------

    def _drain_exception_head(self) -> Optional[dict]:
        """If the ROB head carries an exception, perform the precise rollback:
        the faulting instruction and every younger result are discarded and
        the map table is reconstructed to the state just before the faulting
        dispatch by walking old-tags youngest-first."""
        if not self.rob or not self.rob[0].has_exception:
            return None

        fault = self.rob[0]
        everyone = list(self.rob)  # fault + all younger (it is the head)
        reclaimed = []
        restored = []
        discarded = []

        def rollback_entry(e: RobEntry):
            if e.instr.dest is not None and self.map_table[e.instr.dest] == e.tag:
                restored.append(
                    {"arch": e.instr.dest, "from": e.tag, "to": e.old_tag,
                     "seq": e.seq, "generation": e.generation}
                )
                self.map_table[e.instr.dest] = e.old_tag
            if e.instr.dest is not None:
                self.free_list.append(e.tag)
                reclaimed.append(e.tag)
            discarded.append(
                self._record_squash(e, "exception", fault.seq,
                                    is_faulting=e.seq == fault.seq)
            )

        for e in reversed(everyone[1:]):
            rollback_entry(e)
            self.by_seq.pop(e.seq, None)
            self.checkpoints.pop(e.seq, None)
        rollback_entry(fault)
        self.by_seq.pop(fault.seq, None)
        self.checkpoints.pop(fault.seq, None)
        self.rob = []
        self.free_list.sort()

        record = {
            "fault_seq": fault.seq,
            "discarded": discarded,
            "reclaimed_tags": sorted(reclaimed),
            "restored_mappings": restored,
            "precise_map": list(self.map_table),
            "free_after": sorted(self.free_list),
        }
        self.rollback_log.append(record)
        return record

    def _squash_evidence(self, seq) -> dict:
        insts = self.instances.get(seq, [])
        live = self._lookup(seq)
        return {
            "seq": seq,
            "squashed": sorted(self.squashed),
            "committed": list(self.committed),
            "rob_head": self.rob[0].seq if self.rob else None,
            "current_generation": self.dispatch_generations.get(seq),
            "instances": [
                {"generation": inst["generation"], "status": inst["status"],
                 "wb_matched": inst["wb_matched"], "tag": inst["tag"]}
                for inst in insts
            ],
            "live_entry": live.to_dict() if live is not None else None,
            "squashed_instances": [
                dict(rec) for rec in self.squash_log if rec["seq"] == seq
            ],
        }

    def _exception_evidence(self, entry) -> dict:
        return {
            "fault_seq": entry.seq,
            "map_table": list(self.map_table),
            "rob": [e.to_dict() for e in self.rob],
            "free_list": sorted(self.free_list),
        }

    # -- incremental / full replay -----------------------------------------

    def step(self, ev: Event) -> dict:
        before = self.snapshot()
        action = None
        violation = None
        try:
            action = self.apply(ev)
        except Violation as exc:
            violation = exc.to_dict()
            self.violation = violation
            self.finished = True
        after = self.snapshot()
        self.processed += 1
        step = {
            "index": self.processed - 1,
            "event": {"kind": ev.kind, "seq": ev.seq, "taken": ev.taken, "text": ev.text},
            "description": ev.describe(),
            "before": before,
            "action": action,
            "violation": violation,
            "after": after,
        }
        if violation:
            step["violation"] = violation
        return step

    def run(self, events: list) -> dict:
        steps = []
        for ev in events:
            step = self.step(ev)
            steps.append(step)
            if step["violation"]:
                break
        self.finished = True
        return self.report(steps)

    def report(self, steps: list) -> dict:
        violation_step = next((s for s in steps if s["violation"]), None)
        return {
            "ok": violation_step is None,
            "num_instructions": len(self.instructions),
            "num_events": len(steps),
            "num_phys": self.num_phys,
            "steps": steps,
            "violation": violation_step["violation"] if violation_step else None,
            "violation_step": violation_step["index"] if violation_step else None,
            "final": self.snapshot(),
            "counters": dict(self.counters),
            "rollback_log": self.rollback_log,
            "squash_log": [dict(rec) for rec in self.squash_log],
            "instructions": [
                {"seq": i.seq, "text": i.text, "name": i.name, "op": i.op,
                 "dest": i.dest, "srcs": i.srcs, "is_branch": i.is_branch,
                 "predicted_taken": i.predicted_taken}
                for i in self.instructions
            ],
        }


def simulate(instructions: list, events: list, num_phys: int = DEFAULT_NUM_PHYS) -> dict:
    return Simulator(instructions, num_phys).run(events)
