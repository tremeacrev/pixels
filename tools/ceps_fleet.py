"""Size supervised fleets from the current specification and distribute their work.

The counts are structural proxies, not a measure of semantic difficulty. Each
fleet combines document coverage with independent perspectives and two reviews
of the complete corpus; the parent can request further focused fleets as needed.
"""

from dataclasses import dataclass
import math
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


MIN_WORKERS = 4
MAX_WORKERS = 32
PHASES = {"understand", "plan", "review"}
LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))")
HEADINGS = re.compile(r"^ {0,3}#{1,6}\s+", re.MULTILINE)

FOCUS = {
    "understand": (
        "Map requirements, user intent, and constraints",
        "Trace interactions, dependencies, and lifecycle transitions",
        "Find ambiguous conditions, exceptions, and boundary cases",
        "Challenge implicit assumptions and identify missing requirements",
        "Trace failure handling, recovery, and observable behavior",
        "Assess terminology, examples, and navigation against their requirements",
    ),
    "plan": (
        "Propose the smallest complete improvement and its exact scope",
        "Develop an independent alternative and compare its tradeoffs",
        "Trace affected requirements and changes needed for consistency",
        "Stress-test the proposed approach against exceptions and edge cases",
        "Plan failure handling and preservation of established behavior",
        "Check wording, organization, and links needed to express the change",
    ),
    "review": (
        "Adversarially verify the change against requirements and product intent",
        "Independently challenge assumptions and search for omissions",
        "Check interactions, dependencies, and cross-document contradictions",
        "Exercise boundary conditions, exceptions, and counterexamples",
        "Check regressions, failure handling, and recovery behavior",
        "Audit clarity, terminology, navigation, and metaspecification compliance",
    ),
}
CROSS_CUTTING = {
    "understand": (
        "Read across the entire specification and metaspecification. Map product intent, "
        "shared invariants, and dependencies; find contradictions between subject areas.",
        "Independently challenge the entire specification for missing behavior, ambiguous "
        "exceptions, and conflicts with the writing and organization conventions.",
    ),
    "plan": (
        "Plan integration across the entire specification. Identify every affected invariant, "
        "dependency, and subject area; recommend the smallest cohesive complete change.",
        "Independently challenge the plan against the entire specification and "
        "metaspecification. Develop counterexamples and a competing approach when useful.",
    ),
    "review": (
        "Adversarially review the edits against the entire specification. Look for "
        "contradictions, missed dependencies, regressions, and changes to product intent.",
        "Independently review the complete change for omissions, edge cases, and "
        "metaspecification compliance. Challenge conclusions other reviewers may accept.",
    ),
}


@dataclass(frozen=True)
class Document:
    path: str
    words: int
    headings: int
    cross_file_links: int
    characters: int = 0

    @property
    def weight(self):
        return 1 + self.words / 1200 + self.headings / 12 + self.cross_file_links / 12

    def slices(self, maximum):
        return min(maximum, max(1, math.ceil(self.words / 1200),
                                math.ceil(self.headings / 12),
                                math.ceil(self.cross_file_links / 12)))


def _scope(document, part, pieces):
    if pieces == 1:
        return document.path
    # Word ranges avoid placing a huge paragraph on one worker merely because
    # it occupies one source line. Character ranges also divide link-heavy
    # documents whose compact Markdown can contain few whitespace-separated words.
    if (document.words >= pieces
            and math.ceil(document.words / 1200) >= math.ceil(document.cross_file_links / 12)):
        unit, total = "words", document.words
    else:
        unit, total = "characters", document.characters
    first, last = total * part // pieces + 1, total * (part + 1) // pieces
    return f"{document.path} ({unit} {first}-{last} of {total})"


@dataclass(frozen=True)
class FleetProfile:
    documents: tuple[Document, ...]
    workers: int

    @property
    def summary(self):
        return {
            "documents": len(self.documents),
            "words": sum(document.words for document in self.documents),
            "headings": sum(document.headings for document in self.documents),
            "cross_file_links": sum(document.cross_file_links for document in self.documents),
            "workers": self.workers,
            "max_workers": MAX_WORKERS,
        }

    def assignments(self, task, phase):
        """Return one distinct assignment per worker, preserving the complete task."""
        if phase not in PHASES:
            raise ValueError("Expected understand, plan, or review phase.")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("Expected a nonempty fleet task.")
        focused = self.workers - 2
        groups = [[] for _ in range(focused)]
        loads = [0.0] * focused
        ordered = sorted(self.documents, key=lambda document: (-document.weight, document.path))
        coverage = []
        for document in ordered:
            pieces = document.slices(focused)
            coverage.extend((document, part, pieces) for part in range(pieces))
        coverage.sort(key=lambda item: (-item[0].weight / item[2], item[0].path, item[1]))
        for document, part, pieces in coverage:
            index = min(range(focused), key=lambda item: (loads[item], item))
            groups[index].append(_scope(document, part, pieces))
            loads[index] += document.weight / pieces

        prompts = []
        for index, group in enumerate(groups):
            extra = not group and bool(ordered)
            if extra:
                # Even a tiny corpus gets independent overlapping scrutiny.
                group = [ordered[(index - len(ordered)) % len(ordered)].path]
            focus = FOCUS[phase][index % len(FOCUS[phase])]
            scope = "\n".join(f"- {path}" for path in sorted(group)) or (
                "- Read the available specification/ and metaspecification/ content."
            )
            prompts.append(
                f"Fleet specialist {index + 1}/{self.workers}; phase {phase}.\n"
                f"Your perspective: {focus}.\n"
                + ("Provide an additional independent perspective on this document.\n" if extra else "")
                + "Primary document coverage:\n" + scope + "\n"
                "Read surrounding sections and related documents as needed; preserve context at "
                "scope boundaries and assess the task against the whole product intent. "
                "Report concrete findings, source paths, implications, and recommendations.\n\n"
                "Complete parent task:\n" + task
            )
        for offset, focus in enumerate(CROSS_CUTTING[phase]):
            prompts.append(
                f"Fleet specialist {focused + offset + 1}/{self.workers}; phase {phase}.\n"
                f"Cross-cutting perspective: {focus}\n"
                "Report concrete findings, source paths, implications, and recommendations.\n\n"
                "Complete parent task:\n" + task
            )
        return prompts


def _cross_file_links(path, text):
    count = 0
    for match in LINK.finditer(text):
        try:
            target = urlsplit(match.group(1) or match.group(2))
        except ValueError:
            continue
        if target.scheme or target.netloc or not target.path or target.path.startswith("/"):
            continue
        destination = (path.parent / unquote(target.path)).resolve()
        if destination != path.resolve():
            count += 1
    return count


def build_fleet(repo):
    """Read the current corpus, then size and describe this attempt's fleet."""
    root = Path(repo)
    documents = []
    for directory in ("specification", "metaspecification"):
        for path in sorted((root / directory).rglob("*.md")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            documents.append(Document(
                path.relative_to(root).as_posix(), len(text.split()),
                len(HEADINGS.findall(text)), _cross_file_links(path, text),
                len(text),
            ))
    size = max(
        len(documents),
        math.ceil(sum(document.words for document in documents) / 1200),
        math.ceil(sum(document.headings for document in documents) / 12),
        math.ceil(sum(document.cross_file_links for document in documents) / 12),
    )
    return FleetProfile(tuple(documents), min(MAX_WORKERS, max(MIN_WORKERS, size + 2)))
