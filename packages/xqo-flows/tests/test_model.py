import pytest

from xqo_flows.model import FlowError, check, render, validate
from helpers import edge, flow, node


def simple():
    return flow([node("s", "start", inputs=[{"name": "topic", "default": "x"}]), node("a", "agent", prompt="do {{inputs.topic}}"),
                 node("e", "end", output="{{a.result}}")], [edge("s", "a"), edge("a", "e")])


def test_valid_flow_is_normalized_and_keeps_layout():
    d = validate(simple(), "demo")
    assert d["name"] == "demo" and d["nodes"][0]["meta"]["position"] == {"x": 0, "y": 0}
    assert d["nodes"][1]["data"]["title"] == "a"


@pytest.mark.parametrize("change, msg", [
    (lambda d: d["edges"].append(edge("e", "a")), "end node has no outgoing"),
    (lambda d: d["nodes"].append(node("s2", "start")), "exactly one start"),
    (lambda d: d["edges"].append(edge("a", "zz")), "does not exist"),
    (lambda d: d["nodes"].__setitem__(1, node("a", "agent", prompt="")), "prompt is empty"),
    (lambda d: d["nodes"].append(node("x", "teleport")), "unknown type"),
])
def test_invalid_flows_are_refused(change, msg):
    d = simple()
    change(d)
    with pytest.raises(FlowError, match=msg):
        validate(d, "demo")


def test_loops_and_bad_names_and_ports():
    d = simple()
    d["nodes"].append(node("b", "agent", prompt="b"))
    d["edges"] += [edge("a", "b"), edge("b", "a")]
    with pytest.raises(FlowError, match="loop"):
        validate(d, "demo")
    with pytest.raises(FlowError, match="name"):
        validate(simple(), "runs")
    c = flow([node("s", "start"), node("c", "condition", conditions=[{"key": "yes", "left": "x", "op": "contains", "right": "x"}]),
              node("e", "end")], [edge("s", "c"), edge("c", "e")])
    with pytest.raises(FlowError, match="must name one of its ports: yes, else"):
        validate(c, "demo")


def test_templates_and_conditions():
    scope = {"inputs": {"topic": "Solar"}, "a": {"result": "OK: 42 panels", "status": "done"}}
    assert render("{{inputs.topic}} / {{a}} / {{a.status}} / {{nope.result}}", scope) == "Solar / OK: 42 panels / done / "
    assert check({"left": "{{a.result}}", "op": "contains", "right": "ok"}, scope)
    assert check({"left": "{{a.result}}", "op": "matches", "right": r"^OK:\s+\d+"}, scope)
    assert not check({"left": "{{a.result}}", "op": "equals", "right": "ok"}, scope)
    assert check({"left": "12", "op": "gt", "right": "3"}, scope) and not check({"left": "x", "op": "gt", "right": "3"}, scope)
    assert check({"left": "{{nope}}", "op": "empty"}, scope)
