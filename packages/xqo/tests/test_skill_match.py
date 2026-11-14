"""Skill lookup: role names ('coder', 'tester', 'researcher') used as skills still find the right agent."""
import asyncio

from xqo.agent import Agent, stem


def agent(name, skills):
    return Agent(name, skills, "", llm=None, artifacts=None)


def offer(a, skill):
    return asyncio.run(a.consider_offer(skill, "x"))


def test_stem_meets_role_and_skill_words():
    assert stem("coder") == stem("coding")
    assert stem("tester") == stem("testing")
    assert stem("researcher") == stem("research")
    assert stem("writer") == stem("writing")
    assert stem("qa") == "qa" and stem("css") == "css"


def test_agent_name_is_an_exact_match():
    o = offer(agent("coder", ["python", "coding", "javascript"]), "Coder")
    assert o["match"] == "exact" and o["score"] == 2


def test_role_words_match_skills_partially():
    assert offer(agent("dev", ["python", "coding"]), "coder")["match"] == "partial"
    assert offer(agent("qa1", ["testing", "qa"]), "tester")["match"] == "partial"
    assert offer(agent("analyst", ["research", "data analysis"]), "researcher")["match"] == "partial"
    assert offer(agent("dev", ["python", "coding"]), "copywriting") is None
    assert offer(agent("tester", ["testing"]), "test runner")["match"] == "partial"
