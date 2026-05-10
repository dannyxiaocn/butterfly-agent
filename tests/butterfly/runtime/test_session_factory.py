import pytest

from butterfly.session_engine.session_init import init_session


def _seed_agent(tmp_path):
    agent_base = tmp_path / 'agenthub'
    ent = agent_base / 'demo'
    (ent / 'prompts').mkdir(parents=True)
    (ent / 'prompts' / 'system.md').write_text('sys\n', encoding='utf-8')
    (ent / 'prompts' / 'task.md').write_text('task\n', encoding='utf-8')
    (ent / 'prompts' / 'env.md').write_text('env\n', encoding='utf-8')
    (ent / 'tools.md').write_text('bash\n', encoding='utf-8')
    (ent / 'skills.md').write_text('', encoding='utf-8')
    (ent / 'config.yaml').write_text(
        'name: demo\nmodel: claude-sonnet-4-6\nprovider: anthropic\n',
        encoding='utf-8',
    )
    return agent_base


def test_init_session_seeds_memory_from_agent(tmp_path):
    agent_base = _seed_agent(tmp_path)
    agent_dir = agent_base / 'demo'
    (agent_dir / 'memory').mkdir(parents=True)
    (agent_dir / 'memory.md').write_text('agent primary', encoding='utf-8')
    (agent_dir / 'memory' / 'layer.md').write_text('agent layer', encoding='utf-8')
    (agent_dir / 'playground').mkdir(parents=True)
    (agent_dir / 'playground' / 'seed.txt').write_text('seed', encoding='utf-8')

    init_session(
        's1',
        'demo',
        sessions_base=tmp_path / 'sessions',
        system_sessions_base=tmp_path / '_sessions',
        agent_base=agent_base,
    )

    core = tmp_path / 'sessions' / 's1' / 'core'
    assert (core / 'memory.md').read_text(encoding='utf-8') == 'agent primary'
    assert (core / 'memory' / 'layer.md').read_text(encoding='utf-8') == 'agent layer'
    assert (tmp_path / 'sessions' / 's1' / 'playground' / 'seed.txt').read_text(encoding='utf-8') == 'seed'


def test_init_session_copies_agent_config(tmp_path):
    """init_session copies prompts and config.yaml from agenthub directly."""
    agent_base = _seed_agent(tmp_path)
    init_session(
        's1',
        'demo',
        sessions_base=tmp_path / 'sessions',
        system_sessions_base=tmp_path / '_sessions',
        agent_base=agent_base,
    )
    core = tmp_path / 'sessions' / 's1' / 'core'
    assert (core / 'config.yaml').exists()
    assert (core / 'system.md').read_text(encoding='utf-8').strip() == 'sys'
