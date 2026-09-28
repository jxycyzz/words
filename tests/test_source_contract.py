"""Compare copied domain code against source without importing the desktop runtime."""
import ast
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
DESKTOP=ROOT.parent/'app'
pytestmark=pytest.mark.skipif(not (DESKTOP/'game_state.py').exists(),reason='Desktop source is absent in standalone distribution')


def parsed(path):return ast.parse(path.read_text(encoding='utf-8-sig'))


@pytest.mark.parametrize('filename',['game_state.py','models.py','voice_matching.py','parent_settings.py'])
def test_copied_domain_body_is_identical(filename):
    original=parsed(DESKTOP/filename)
    copied=parsed(ROOT/'backend/domain'/filename)
    assert ast.dump(original)==ast.dump(copied)


@pytest.mark.parametrize('name',['retention_probability','mastery_percent','review_score','list_review_words','_weighted_review_sample','_unique_word_ids','_ordered_required_ids','_trim_daily_review_ids','_fill_daily_review_ids'])
def test_selection_methods_are_identical_except_injected_calendar(name):
    original=next(n for n in parsed(DESKTOP/'repositories.py').body if isinstance(n,ast.ClassDef) and n.name=='WordRepository')
    original=next(n for n in original.body if isinstance(n,ast.FunctionDef) and n.name==name)
    copied=next(n for n in parsed(ROOT/'backend/domain/review_selection.py').body if isinstance(n,ast.ClassDef))
    copied=next(n for n in copied.body if isinstance(n,ast.FunctionDef) and n.name==name)
    class Calendar(ast.NodeTransformer):
        def visit_Call(self,node):
            if ast.unparse(node)=='date.today()':return ast.parse('self.target_day',mode='eval').body
            return self.generic_visit(node)
    assert ast.dump(Calendar().visit(original))==ast.dump(copied)


@pytest.mark.parametrize('name',['AIService','QwenASRService'])
def test_provider_protocol_and_prompts_are_identical(name):
    original=next(n for n in parsed(DESKTOP/'services.py').body if isinstance(n,ast.ClassDef) and n.name==name)
    copied=next(n for n in parsed(ROOT/'backend/domain/ai_services.py').body if isinstance(n,ast.ClassDef) and n.name==name)
    assert ast.dump(original)==ast.dump(copied)
