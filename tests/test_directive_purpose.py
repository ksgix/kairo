"""Directives as purpose.

A directive is Kairo's lasting purpose: a statement and the operator's description
of what it covers. It is never a task list and creating one executes nothing.
Implementation packages are not bound to directives: one is available when it is
enabled and its requirements are met.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from kairo import Environment
from kairo.implementations import Implementations
from kairo.instructions import INSTRUCTIONS, cognition_request
from kairo.ipc import OPS
from kairo.runtime import DIRECTIVE_DESCRIPTION, OperatorRejected
from kairo.situation import LIMITS, build_situation
from test_dashboard import DashboardCase
from test_implementations import ImplCase, pkg, tool
from test_work import WorkCase, create

DESCRIPTION = ("Continuously identify and pursue worthwhile improvements to the project: "
               "documentation, code quality, tests and tooling. Kairo decides the concrete "
               "work itself; nothing here is a task.")
ECHO = "import json, sys\nprint(json.dumps(json.load(sys.stdin)))\n"


class DirectiveRecordTest(WorkCase):
    def test_statement_and_description_are_persisted_with_history(self):
        rt = self.runtime()
        d = rt.add_directive("  Improve the project  ", f"  {DESCRIPTION}  ")
        record = rt.memory.get("directive", d.id)
        self.assertEqual((record["statement"], record["description"]),
                         ("Improve the project", DESCRIPTION))
        self.assertEqual((record["origin"], record["active"]), ("operator", True))
        self.assertEqual([h["event"] for h in record["history"]], ["created"])
        [listed] = rt.directive_list()["directives"]
        self.assertEqual(listed["description"], DESCRIPTION)

    def test_description_is_required_and_bounded(self):
        rt = self.runtime()
        for description in ("", "   ", "x" * (DIRECTIVE_DESCRIPTION + 1)):
            with self.subTest(length=len(description)):
                with self.assertRaises(OperatorRejected):
                    rt.add_directive("Improve the project", description)
        rt.add_directive("Improve the project", "x" * DIRECTIVE_DESCRIPTION)
        with self.assertRaises(OperatorRejected):  # same statement, active: refused
            rt.add_directive("improve  the PROJECT", DESCRIPTION)
        self.assertEqual(len(rt.memory.all("directive")), 1)

    def test_creating_a_directive_establishes_purpose_only(self):
        rt = self.runtime()
        rt.start()
        before = (rt.memory.count("work"), rt.memory.count("action"))
        rt.add_directive("Improve the project", DESCRIPTION)
        self.assertEqual((rt.memory.count("work"), rt.memory.count("action")), before)

    def test_directives_are_never_edited_only_deactivated_and_activated(self):
        rt = self.runtime()
        d = rt.add_directive("Improve the project", DESCRIPTION)
        rt.set_directive_active(d.id, False)
        rt.set_directive_active(d.id, True)
        record = rt.memory.get("directive", d.id)
        self.assertEqual((record["statement"], record["description"]),
                         ("Improve the project", DESCRIPTION))
        self.assertEqual([h["event"] for h in record["history"]],
                         ["created", "deactivated", "activated"])
        self.assertFalse({op for op in OPS if op.startswith("directive.")}
                         - {"directive.add", "directive.activate", "directive.deactivate"})

    def test_a_directive_from_before_descriptions_still_works(self):
        rt = self.runtime()
        legacy = {"statement": "Keep the host healthy", "active": True, "id": "legacy-1",
                  "created_at": 1.0, "origin": "operator", "history": []}
        rt.memory.put("directive", "legacy-1", legacy)
        rt.start()
        [item] = build_situation(rt.context())["directives"]["active"]
        self.assertEqual((item["statement"], item["description"]),
                         ("Keep the host healthy", None))
        self.assertIsNone(rt.directive_list()["directives"][0]["description"])


class DirectiveInContextTest(WorkCase):
    def test_cognition_sees_the_description_as_operator_words(self):
        rt = self.runtime()
        long_description = DESCRIPTION + " " + "Scope details. " * 200  # ~3,200 characters
        d = rt.add_directive("Improve the project", long_description[:DIRECTIVE_DESCRIPTION])
        rt.start()
        s = build_situation(rt.context())
        [item] = s["directives"]["active"]
        self.assertEqual(item["id"], d.id)
        self.assertEqual(item["description"], d.description)  # whole, not cut at 2,000
        self.assertGreater(len(item["description"]), LIMITS.text)
        self.assertIn("Directives are the operator's words", INSTRUCTIONS)
        self.assertIn("not facts and not task lists", INSTRUCTIONS)
        self.assertEqual(s["directives"]["source"], "runtime records, set by the operator")


class UnboundImplementationsTest(ImplCase):
    def test_a_package_is_available_without_naming_any_directive(self):
        pkg(self.root, "docs", files={"tools/run.py": ECHO}, tools=[tool("run")])
        rt = self.runtime()
        rt.start()
        entry = {e.id: e for e in rt.environment.implementations.catalog()}["docs"]
        self.assertEqual(entry.state, "available")
        self.assertIn("impl.docs.run", rt.environment.actions())
        [item] = build_situation(rt.context())["capabilities"]["implementations"]["items"]
        self.assertNotIn("serves", item)
        status = rt.status()
        self.assertEqual(status["implementations"][0]["state"], "available")
        self.assertNotIn("serves", status["implementations"][0])

    def test_a_manifest_naming_directives_is_refused(self):
        pkg(self.root, "old", directives=["d1"])
        self.assertBroken("old", "directives")

    def test_directives_do_not_list_implementations(self):
        pkg(self.root, "docs", files={"tools/run.py": ECHO}, tools=[tool("run")])
        rt = self.runtime()
        rt.add_directive("Improve the project", DESCRIPTION)
        [seen] = build_situation(rt.context())["directives"]["active"]
        self.assertNotIn("implementations", seen)


class DashboardDirectiveFlowTest(DashboardCase):
    def test_add_read_toggle_through_the_dashboard(self):
        runtime = self.ready()
        added = self.ok("POST", "/api/directives", {"statement": "Improve the project",
                                                    "description": DESCRIPTION})["directive"]
        self.assertEqual(added["description"], DESCRIPTION)
        self.assertEqual(runtime.memory.get("directive", added["id"])["description"], DESCRIPTION)
        [listed] = self.ok("GET", "/api/directives")["directives"]
        self.assertEqual((listed["statement"], listed["description"]),
                         ("Improve the project", DESCRIPTION))
        [seen] = self.ok("GET", "/api/situation")["directives"]["active"]
        self.assertEqual(seen["description"], DESCRIPTION)
        self.fails(self.post("/api/directives", {"statement": "Only a statement"}),
                   400, "invalid_params")
        self.ok("POST", "/api/directives/deactivate", {"id": added["id"]})
        self.assertEqual(runtime.memory.get("directive", added["id"])["description"], DESCRIPTION)
        self.assertEqual((runtime.memory.count("work"), runtime.memory.count("action")), (0, 0))

    def test_the_directive_page_shows_purpose_not_a_task_list(self):
        js = (__import__("kairo.dashboard", fromlist=["STATIC"]).STATIC / "app.js").read_text()
        page = js[js.index("function renderDirectives"):js.index("function attemptsTable")]
        self.assertIn('api("/api/directives", {statement: statement.value, '
                      'description: description.value})', page)
        for shown in ("d.description", "Open work for it", "History",
                      "creates no work", "reassesses with it from its next cycle",
                      "required: true"):
            self.assertIn(shown, page)
        self.assertNotRegex(page, r"innerHTML|insertAdjacentHTML")


HARNESS = Path(__file__).resolve().parent / "dashboard_dom.mjs"


@unittest.skipUnless(shutil.which("node"), "needs node to run the dashboard's own app.js")
class DirectiveFormEndToEndTest(DashboardCase):
    """The user-visible path, with the dashboard's real app.js in a minimal DOM (not
    a browser): the form, the request, persistence, the directive page, and the
    situation and prompt cognition is given."""

    def setUp(self):
        super().setUp()
        self.impls = self.dir / "implementations"
        self.impls.mkdir()
        self.runtime = self.launch(environment=Environment(Implementations(self.impls, "all")))
        self.serve()

    def ui(self, *steps):
        out = subprocess.run(["node", str(HARNESS), str(self.port), str(self.token_file),
                              json.dumps(list(steps))], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout)
        self.assertEqual(result["errors"], [])
        return result["snapshots"]

    def test_from_the_form_to_what_cognition_is_given(self):
        # 1. The form asks for both, and says what a directive is.
        form = self.ui({"page": "directives"}, {"snapshot": "form"})["form"]
        controls = {c["id"]: c for c in form["controls"]}
        self.assertEqual((controls["directive"]["tag"], controls["directive"]["required"],
                          controls["directive"]["maxlength"]), ("input", True, "500"))
        self.assertEqual((controls["directive-description"]["tag"],
                          controls["directive-description"]["required"],
                          controls["directive-description"]["maxlength"]),
                         ("textarea", True, str(DIRECTIVE_DESCRIPTION)))
        labels = {lab["for"]: lab["text"] for lab in form["labels"]}
        self.assertTrue(labels["directive"].startswith("Statement"))
        self.assertTrue(labels["directive-description"].startswith("Description"))
        for said in ("lasting area of responsibility", "creates no work",
                     "reassesses with it from its next cycle"):
            self.assertIn(said, form["text"])

        # 2. A statement alone is refused, by the page and by the API; nothing is stored.
        missing = self.ui({"page": "directives"}, {"fill": {"directive": "Only a statement"}},
                          {"click": "Add directive"}, {"snapshot": "missing"})["missing"]
        self.assertIn("Both a statement and a description are needed.", missing["text"])
        self.login()
        self.fails(self.post("/api/directives", {"statement": "Only a statement"}),
                   400, "invalid_params")
        self.assertEqual(self.runtime.memory.all("directive"), [])

        # 3. Both are submitted together and persisted whole.
        statement = "Continuously improve the project into a maintainable whole"
        description = ("<scope:start> " + "Worthwhile improvements to code, docs and tests, "
                       "chosen by Kairo itself. " * 50 + "<scope:end>")
        self.assertGreater(len(description), 3000)
        created = self.ui({"page": "directives"},
                          {"fill": {"directive": statement, "directive-description": description}},
                          {"click": "Add directive"}, {"snapshot": "created"})["created"]
        [record] = self.runtime.memory.all("directive")
        self.assertEqual((record["statement"], record["description"]), (statement, description))
        self.assertEqual((record["origin"], record["active"]), ("operator", True))

        # 4. The directive page shows it, description in full.
        [card] = created["cards"]
        self.assertIn(statement, card["heading"])
        self.assertEqual(card["description"], description)
        self.assertIn("active", card["heading"])
        self.assertIn("set by operator", card["text"])
        self.assertIn("History (1)", card["text"])

        # 5. Cognition is given both, in full (no lower layer cuts the description).
        [seen] = self.ok("GET", "/api/situation")["directives"]["active"]
        self.assertEqual((seen["statement"], seen["description"]), (statement, description))
        self.assertNotIn("description_shortened", seen)
        prompt = cognition_request(self.runtime.context()).prompt
        self.assertIn(json.dumps(statement), prompt)
        self.assertIn(json.dumps(description), prompt)

        # 6. Work linked to the directive shows on its card.
        did = record["id"]
        self.runtime.work.apply([create("w", "Tidy the README", directive_id=did)])
        linked = self.ui({"page": "directives"}, {"snapshot": "linked"})["linked"]
        [card] = linked["cards"]
        self.assertIn("Tidy the README", card["text"])
        self.assertEqual(self.runtime.memory.count("action"), 0)  # nothing was executed


if __name__ == "__main__":
    unittest.main()
