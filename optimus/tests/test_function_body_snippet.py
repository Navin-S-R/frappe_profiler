# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Self-time hot-path findings (Phase-1 'Slow Hot Path' with no deeper user
frame) should show the WHOLE function body, not a ±2-line peek — Phase-1
sampling can't pinpoint the hot line, but the function is the relevant unit and
the tool already has the source. _read_function_body_snippet reads def→end;
_expand_self_time_snippets swaps it in for the no-deeper-frame findings.
"""

from optimus import renderer


def _func_finding(filename, def_lineno, *, drilldown_chain, finding_type="Slow Hot Path"):
	return {
		"finding_type": finding_type,
		"technical_detail": {
			"callsite": {
				"filename": filename,
				"lineno": def_lineno,
				"function": "bg_recheck_users",
				"source_snippet": [{"lineno": def_lineno, "content": "def x():"}],  # the ±2 stand-in
			},
			"drilldown_chain": drilldown_chain,
		},
	}


def _snippet(finding):
	return finding["technical_detail"]["callsite"]["source_snippet"]


_SAMPLE = (
	"import os\n"                       # 1
	"\n"                                 # 2
	"def bg_recheck_users(doc=None):\n"  # 3  <- def
	"    for i in range(15):\n"          # 4
	"        try:\n"                      # 5
	"            do_work(i)\n"            # 6
	"        except Exception:\n"         # 7
	"            pass\n"                  # 8
	"    return None\n"                  # 9
	"\n"                                 # 10
	"def next_function():\n"             # 11
	"    pass\n"                          # 12
)


class TestReadFunctionBody:
	def test_reads_def_to_end_of_function(self, tmp_path):
		src = tmp_path / "m.py"
		src.write_text(_SAMPLE)
		body = renderer._read_function_body_snippet(str(src), 3)
		linenos = [r["lineno"] for r in body]
		# def (3) through return (9) + the trailing blank (10); stops before
		# the dedented `def next_function` (11).
		assert linenos[0] == 3
		assert 9 in linenos
		assert 11 not in linenos
		assert any("for i in range(15)" in r["content"] for r in body)

	def test_caps_at_max_lines(self, tmp_path):
		src = tmp_path / "big.py"
		lines = ["def f():"] + [f"    x{i} = {i}" for i in range(100)]
		src.write_text("\n".join(lines) + "\n")
		body = renderer._read_function_body_snippet(str(src), 1, max_lines=10)
		assert len(body) == 10

	def test_unreadable_returns_none(self):
		assert renderer._read_function_body_snippet("/nonexistent/x.py", 3) is None

	def test_out_of_range_returns_none(self, tmp_path):
		src = tmp_path / "m.py"
		src.write_text("a\nb\n")
		assert renderer._read_function_body_snippet(str(src), 99) is None


class TestExpandSelfTimeSnippets:
	def test_empty_chain_slow_hot_path_gets_full_body(self, tmp_path):
		src = tmp_path / "m.py"
		src.write_text(_SAMPLE)
		f = _func_finding(str(src), 3, drilldown_chain=[])
		renderer._expand_self_time_snippets([f], file_cache=None)
		linenos = [r["lineno"] for r in _snippet(f)]
		assert linenos[0] == 3 and 9 in linenos  # full body, not the 1-row stand-in
		assert len(_snippet(f)) > 1

	def test_non_empty_chain_left_unchanged(self, tmp_path):
		src = tmp_path / "m.py"
		src.write_text(_SAMPLE)
		f = _func_finding(str(src), 3, drilldown_chain=[{"function": "inner", "lineno": 6}])
		renderer._expand_self_time_snippets([f], file_cache=None)
		assert _snippet(f) == [{"lineno": 3, "content": "def x():"}]  # untouched

	def test_other_finding_types_left_unchanged(self, tmp_path):
		src = tmp_path / "m.py"
		src.write_text(_SAMPLE)
		f = _func_finding(str(src), 3, drilldown_chain=[], finding_type="N+1 Query")
		renderer._expand_self_time_snippets([f], file_cache=None)
		assert _snippet(f) == [{"lineno": 3, "content": "def x():"}]

	def test_unreadable_file_leaves_snippet(self):
		f = _func_finding("/nonexistent/x.py", 3, drilldown_chain=[])
		renderer._expand_self_time_snippets([f], file_cache=None)
		assert _snippet(f) == [{"lineno": 3, "content": "def x():"}]
