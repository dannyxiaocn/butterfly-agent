import nutshell.runtime.meta_session as ms
from nutshell.runtime.session_factory import init_session


def test_init_session_seeds_memory_from_meta_session(tmp_path):
    entity_base = tmp_path / 'entity'
    (entity_base / 'demo').mkdir(parents=True)
    (entity_base / 'demo' / 'agent.yaml').write_text(
        'name: demo\n'
        'model: claude-sonnet-4-6\n'
        'provider: anthropic\n'
        'tools: []\n'
        'skills: []\n',
        encoding='utf-8',
    )
    (tmp_path / 'sessions' / 'demo_meta' / 'core' / 'memory').mkdir(parents=True)
    (tmp_path / 'sessions' / 'demo_meta' / 'playground').mkdir(parents=True)
    (tmp_path / 'sessions' / 'demo_meta' / 'core' / 'memory.md').write_text('meta primary', encoding='utf-8')
    (tmp_path / 'sessions' / 'demo_meta' / 'core' / 'memory' / 'layer.md').write_text('meta layer', encoding='utf-8')
    (tmp_path / 'sessions' / 'demo_meta' / 'playground' / 'seed.txt').write_text('seed', encoding='utf-8')

    ms._SESSIONS_DIR = tmp_path / 'sessions'
    init_session('s1', 'demo', sessions_base=tmp_path / 'sessions', system_sessions_base=tmp_path / '_sessions', entity_base=entity_base)

    core = tmp_path / 'sessions' / 's1' / 'core'
    assert (core / 'memory.md').read_text(encoding='utf-8') == 'meta primary'
    assert (core / 'memory' / 'layer.md').read_text(encoding='utf-8') == 'meta layer'
    assert (tmp_path / 'sessions' / 's1' / 'playground' / 'seed.txt').read_text(encoding='utf-8') == 'seed'
