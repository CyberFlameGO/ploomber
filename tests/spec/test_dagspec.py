import shutil
from unittest.mock import Mock
import sys
import os
from datetime import timedelta, datetime
import numpy as np
import pandas as pd
from pathlib import Path
import pytest
import yaml
from conftest import _path_to_tests, fixture_tmp_dir
import getpass
from copy import deepcopy

import jupytext
import nbformat
import jupyter_client
from sqlalchemy import create_engine

from ploomber.spec import dagspec
from ploomber.spec.dagspec import DAGSpec, Meta
from ploomber.util.dotted_path import load_dotted_path
from ploomber.tasks import PythonCallable
from ploomber.clients import db
from ploomber.env import expand
from ploomber.tasks import SQLScript
from ploomber import exceptions
from ploomber.executors import Serial, Parallel
from ploomber.products import MetaProduct


def create_engine_with_schema(schema):
    def fake_create_engine(*args, **kwargs):
        if 'sqlite' in args[0]:
            return create_engine(*args, **kwargs)
        else:
            return create_engine(
                *args,
                **kwargs,
                connect_args=dict(options=f'-c search_path={schema}'))

    return fake_create_engine


@fixture_tmp_dir(_path_to_tests() / 'assets' / 'pipeline-sql')
def tmp_pipeline_sql():
    pass


@fixture_tmp_dir(_path_to_tests() / 'assets' / 'pipeline-r')
def tmp_pipeline_r():
    pass


@fixture_tmp_dir(_path_to_tests() / 'assets' /
                 'pipeline-sql-products-in-source')
def tmp_pipeline_sql_products_in_source():
    pass


def to_ipynb(dag_spec):
    for source in ['load.py', 'clean.py', 'plot.py']:
        nb = jupytext.read(source)
        Path(source).unlink()

        k = jupyter_client.kernelspec.get_kernel_spec('python3')

        nb.metadata.kernelspec = {
            "display_name": k.display_name,
            "language": k.language,
            "name": 'python3'
        }

        nbformat.write(nb, source.replace('.py', '.ipynb'))

    for task in dag_spec['tasks']:
        task['source'] = task['source'].replace('.py', '.ipynb')

    return dag_spec


def tasks_list(dag_spec):
    tasks = dag_spec['tasks']

    # we have to pop this, since a list of tasks gets meta default params
    # which extracts upstream
    for t in tasks:
        t.pop('upstream', None)

    return tasks


def remove_task_class(dag_spec):
    for task in dag_spec['tasks']:
        del task['class']

    return dag_spec


def extract_upstream(dag_spec):
    dag_spec['meta']['extract_upstream'] = True

    for task in dag_spec['tasks']:
        task.pop('upstream', None)

    return dag_spec


def extract_product(dag_spec):
    dag_spec['meta']['extract_product'] = True

    for task in dag_spec['tasks']:
        task.pop('product', None)

    return dag_spec


def get_all_products(dag):
    products = []

    for task in dag.values():
        if isinstance(task.product, MetaProduct):
            products.extend(list(task.product))
        else:
            products.append(task.product)

    return products


def test_error_if_missing_tasks_key():
    with pytest.raises(KeyError):
        DAGSpec({'some_key': None})


def test_validate_top_level_keys():
    with pytest.raises(KeyError):
        DAGSpec({'tasks': [], 'invalid_key': None})


def test_validate_meta_keys():
    with pytest.raises(KeyError):
        DAGSpec({'tasks': [], 'meta': {'invalid_key': None}})


def test_python_callables_spec(tmp_directory, add_current_to_sys_path):
    Path('test_python_callables_spec.py').write_text("""
def task1(product):
    pass
""")

    spec = DAGSpec({
        'tasks': [
            {
                'source': 'test_python_callables_spec.task1',
                'product': 'some_file.csv'
            },
        ],
        'meta': {
            'extract_product': False,
            'extract_upstream': False
        }
    })

    dag = spec.to_dag()
    assert isinstance(dag['task1'], PythonCallable)


def test_python_callables_with_extract_upstream(tmp_directory):
    spec = DAGSpec({
        'tasks': [
            {
                'source': 'test_pkg.callables.root',
                'product': 'root.csv'
            },
            {
                'source': 'test_pkg.callables.a',
                'product': 'a.csv'
            },
            {
                'source': 'test_pkg.callables.b',
                'product': 'b.csv'
            },
        ],
        'meta': {
            'extract_product': False,
            'extract_upstream': True
        }
    })

    dag = spec.to_dag()

    dag.build()

    assert set(dag) == {'a', 'b', 'root'}
    assert not dag['root'].upstream
    assert set(dag['a'].upstream) == {'root'}
    assert set(dag['b'].upstream) == {'root'}


@pytest.mark.parametrize('executor, expected', [
    ['serial', Serial],
    ['parallel', Parallel],
])
def test_custom_serializer(executor, expected):
    dag = DAGSpec({
        'tasks': [{
            'source': 'test_pkg.callables.root',
            'product': 'root.csv'
        }],
        'executor':
        executor,
    }).to_dag()

    assert isinstance(dag.executor, expected)


@pytest.mark.parametrize('processor', [
    to_ipynb, tasks_list, remove_task_class, extract_upstream, extract_product
])
def test_notebook_spec(processor, tmp_nbs):
    Path('output').mkdir()

    with open('pipeline.yaml') as f:
        dag_spec = yaml.load(f, Loader=yaml.SafeLoader)

    dag_spec = processor(dag_spec)

    dag = DAGSpec(dag_spec).to_dag()
    dag.build()


def test_notebook_spec_nested(tmp_nbs_nested):
    Path('output').mkdir()
    dag = DAGSpec('pipeline.yaml').to_dag()
    dag.build()


def test_loads_env_if_exists(tmp_nbs):
    Path('env.yaml').write_text("{'a': 1}")
    spec = DAGSpec('pipeline.yaml')
    assert spec.env.a == 1


def test_loads_env_and_replaces_keys(tmp_nbs):
    Path('env.yaml').write_text("{'a': 1, 'b': 2}")
    spec = DAGSpec('pipeline.yaml', env=dict(b=3))
    assert spec.env.a == 1
    assert spec.env.b == 3


def test_prioritizes_local_env_over_sibling_env(tmp_nbs):
    files = os.listdir()
    sub_dir = Path('subdir')
    sub_dir.mkdir()

    for f in files:
        Path(f).rename(sub_dir / f)

    Path('env.yaml').write_text("{'a': 100}")
    Path('subdir', 'env.yaml').write_text("{'a': 1}")
    spec = DAGSpec('subdir/pipeline.yaml')
    assert spec.env.a == 100


def test_loads_env_in_default_location_if_loading_from_dict(tmp_nbs):
    Path('env.yaml').write_text("{'a': 1}")

    with open('pipeline.yaml') as f:
        d = yaml.safe_load(f)

    spec = DAGSpec(d)
    assert set(spec.env) == {'user', 'cwd', 'a', 'root'}


def test_notebook_spec_w_location(tmp_nbs, add_current_to_sys_path):
    Path('output').mkdir()
    dag = DAGSpec('pipeline-w-location.yaml').to_dag()
    dag.build()


def test_error_if_location_is_not_a_callable(tmp_directory,
                                             add_current_to_sys_path,
                                             no_sys_modules_cache):
    Path('test_error_if_location_is_not_a_callable.py').write_text("""
make_dag = 1
""")

    spec = DAGSpec(
        {'location': 'test_error_if_location_is_not_a_callable.make_dag'})

    with pytest.raises(TypeError) as excinfo:
        spec.to_dag()

    expected = ("Error loading dotted path 'test_error_if_"
                "location_is_not_a_callable.make_dag'. Expected a "
                "callable object (i.e., some kind of function). Got 1 "
                "(an object of type: int)")
    assert str(excinfo.value) == expected


def test_error_if_location_returns_none(tmp_directory, add_current_to_sys_path,
                                        no_sys_modules_cache):
    Path('test_error_if_location_is_not_a_callable.py').write_text("""
def make_dag():
    return None
""")

    spec = DAGSpec(
        {'location': 'test_error_if_location_is_not_a_callable.make_dag'})

    with pytest.raises(TypeError) as excinfo:
        spec.to_dag()

    expected = ("Error calling dotted path 'test_error_if_location_is_"
                "not_a_callable.make_dag'. Expected a value but got None")
    assert str(excinfo.value) == expected


def test_file_spec_resolves_sources_location(tmp_nbs):
    spec = DAGSpec('pipeline.yaml')
    absolute = str(tmp_nbs.resolve())
    assert all(str(t['source']).startswith(absolute) for t in spec['tasks'])


@pytest.mark.parametrize(
    'spec',
    [
        {
            'tasks': [{
                'source': 'task.py',
                'product': 'out.ipynb'
            }]
        },
        {
            'tasks': [{
                'source': 'task.py',
                'product': 'out.ipynb',
                'name': 'task-',
                'grid': {
                    'param': [1, 2]
                }
            }]
        },
        {
            'tasks': [{
                'source': 'task.py',
                'product': {
                    'nb': 'out.ipynb',
                    'data': 'data.csv'
                }
            }]
        },
    ],
)
@pytest.mark.parametrize('base, cwd', [
    ['.', '.'],
    ['.', 'dir'],
    ['dir', '.'],
])
# TODO try metyaproduct
def test_init_from_file_resolves_source_location(tmp_directory, spec, base,
                                                 cwd):
    """
    DAGSpec resolves sources and products to absolute values, this process
    should be independent of the current working directory and ignore the
    existence of other pipeline.yaml in parent directories
    """
    Path('dir').mkdir()
    Path('pipeline.yaml').touch()
    Path('dir', 'pipeline.yaml').touch()

    base = Path(base)

    Path(base, 'task.py').write_text("""
# + tags = ["parameters"]
upstream = None
# -
""")
    Path(base, 'pipeline.yaml').write_text(yaml.dump(spec))
    path_to_pipeline = Path(base, 'pipeline.yaml').resolve()

    os.chdir(cwd)

    dag = DAGSpec(path_to_pipeline).to_dag()
    absolute = str(Path(tmp_directory, base).resolve())

    assert all(
        [str(dag[name].source.loc).startswith(absolute) for name in list(dag)])
    assert all([
        str(product).startswith(absolute) for product in get_all_products(dag)
    ])


@pytest.mark.parametrize(
    'spec',
    [
        {
            'tasks': [{
                'source': 'task.py',
                'product': 'out.ipynb'
            }]
        },
        {
            'tasks': [{
                'source': 'task.py',
                'product': 'out.ipynb',
                'name': 'task-',
                'grid': {
                    'param': [1, 2]
                }
            }]
        },
        {
            'tasks': [{
                'source': 'task.py',
                'product': {
                    'nb': 'out.ipynb',
                    'data': 'data.csv'
                }
            }]
        },
    ],
)
@pytest.mark.parametrize('base, cwd', [
    ['.', '.'],
    ['.', 'dir'],
    ['.', 'src'],
    ['.', 'src/pkg'],
    ['dir', '.'],
    ['dir', 'dir'],
    ['dir', 'dir/src'],
    ['dir', 'dir/src/pkg'],
])
def test_init_from_file_resolves_source_location_in_pkg_structure(
        tmp_directory, spec, base, cwd):
    Path('dir').mkdir()

    base = Path(base)
    Path(base, 'setup.py').touch()

    Path(base, 'task.py').write_text("""
# + tags = ["parameters"]
upstream = None
# -
""")

    pkg = Path(base, 'src', 'pkg')
    pkg.mkdir(parents=True)
    path_to_pipeline = Path(pkg, 'pipeline.yaml').resolve()
    path_to_pipeline.write_text(yaml.dump(spec))

    os.chdir(cwd)

    dag = DAGSpec(path_to_pipeline).to_dag()
    absolute = str(Path(tmp_directory, base).resolve())
    assert all(
        [str(dag[name].source.loc).startswith(absolute) for name in list(dag)])
    assert all([
        str(product).startswith(absolute) for product in get_all_products(dag)
    ])


@pytest.mark.parametrize(
    'chdir, dir_',
    [
        # test with the current directory
        ['.', '.'],
        # and one level up
        ['..', 'content'],
    ])
def test_spec_from_directory(chdir, dir_, tmp_nbs_no_yaml):
    os.chdir(chdir)

    Path('output').mkdir()

    dag = DAGSpec.from_directory(dir_).to_dag()
    assert list(dag) == ['load', 'clean', 'plot']


@pytest.mark.parametrize('touch', [False, True])
def test_spec_from_non_existing_directory(touch, tmp_directory):
    if touch:
        Path('not_a_directory').touch()

    with pytest.raises(NotADirectoryError):
        DAGSpec.from_directory('not_a_directory')


def test_spec_from_files(tmp_nbs_no_yaml):
    dag = DAGSpec.from_files(['load.py', 'clean.py', 'plot.py']).to_dag()
    assert list(dag) == ['load', 'clean', 'plot']


def test_spec_glob_pattern(tmp_nbs_no_yaml):
    # directory should be ignored
    Path('output').mkdir()
    # if passed a string, it's interpreted as a glob-like pattern
    dag = DAGSpec.from_files('load.py').to_dag()

    assert list(dag) == ['load']


def test_spec_invalid_glob_pattern(tmp_nbs_no_yaml):
    Path('some_invalid_script.sh').touch()

    with pytest.raises(ValueError) as excinfo:
        DAGSpec.from_files('*')

    assert ('Cannot instantiate DAGSpec from files with invalid extensions'
            in str(excinfo.value))


def _random_date_from(date, max_days, n):
    return [
        date + timedelta(days=int(days))
        for days in np.random.randint(0, max_days, n)
    ]


def test_postgres_sql_spec(tmp_pipeline_sql, pg_client_and_schema,
                           add_current_to_sys_path, monkeypatch):
    _, schema = pg_client_and_schema

    with open('pipeline-postgres.yaml') as f:
        dag_spec = yaml.load(f, Loader=yaml.SafeLoader)

    # clients for this pipeline are initialized without custom create_engine
    # args but we need to set the default schema, mock the call so it
    # includes that info
    monkeypatch.setattr(db, 'create_engine', create_engine_with_schema(schema))

    dates = _random_date_from(datetime(2016, 1, 1), 365, 100)
    df = pd.DataFrame({
        'customer_id': np.random.randint(0, 5, 100),
        'value': np.random.rand(100),
        'purchase_date': dates
    })
    loader = load_dotted_path(dag_spec['clients']['SQLScript'])
    client = loader()
    df.to_sql('sales', client.engine, if_exists='replace')
    client.engine.dispose()

    dag = DAGSpec(dag_spec).to_dag()

    # FIXME: this does no show the custom Upstream key missing error
    dag.build()

    assert not dag['load'].upstream
    assert list(dag['filter'].upstream.keys()) == ['load']
    assert list(dag['transform'].upstream.keys()) == ['filter']


def test_sql_spec_w_products_in_source(tmp_pipeline_sql_products_in_source,
                                       add_current_to_sys_path):
    with open('pipeline.yaml') as f:
        dag_spec = yaml.load(f, Loader=yaml.SafeLoader)

    dates = _random_date_from(datetime(2016, 1, 1), 365, 100)
    df = pd.DataFrame({
        'customer_id': np.random.randint(0, 5, 100),
        'value': np.random.rand(100),
        'purchase_date': dates
    })
    loader = load_dotted_path(dag_spec['clients']['SQLScript'])
    client = loader()
    df.to_sql('sales', client.engine, if_exists='replace')
    client.engine.dispose()

    dag = DAGSpec(dag_spec).to_dag()
    dag.build()


@pytest.mark.parametrize('spec',
                         ['pipeline-sqlite.yaml', 'pipeline-sqlrelation.yaml'])
def test_sqlite_sql_spec(spec, tmp_pipeline_sql, add_current_to_sys_path):
    with open(spec) as f:
        dag_spec = yaml.load(f, Loader=yaml.SafeLoader)

    dates = _random_date_from(datetime(2016, 1, 1), 365, 100)
    df = pd.DataFrame({
        'customer_id': np.random.randint(0, 5, 100),
        'value': np.random.rand(100),
        'purchase_date': dates
    })
    loader = load_dotted_path(dag_spec['clients']['SQLScript'])
    client = loader()
    df.to_sql('sales', client.engine)
    client.engine.dispose()

    dag = DAGSpec(dag_spec).to_dag()

    # FIXME: this does no show the custom Upstream key missing error
    dag.build()

    assert not dag['load'].upstream
    assert list(dag['filter'].upstream.keys()) == ['load']
    assert list(dag['transform'].upstream.keys()) == ['filter']


def test_mixed_db_sql_spec(tmp_pipeline_sql, add_current_to_sys_path,
                           pg_client_and_schema, monkeypatch):
    _, schema = pg_client_and_schema

    with open('pipeline-multiple-dbs.yaml') as f:
        dag_spec = yaml.load(f, Loader=yaml.SafeLoader)

    # clients for this pipeline are initialized without custom create_engine
    # args but we need to set the default schema, mock the call so it
    # includes that info
    monkeypatch.setattr(db, 'create_engine', create_engine_with_schema(schema))

    dates = _random_date_from(datetime(2016, 1, 1), 365, 100)
    df = pd.DataFrame({
        'customer_id': np.random.randint(0, 5, 100),
        'value': np.random.rand(100),
        'purchase_date': dates
    })
    # make sales data for pg and sqlite
    loader = load_dotted_path(dag_spec['clients']['PostgresRelation'])
    client = loader()
    df.to_sql('sales', client.engine, if_exists='replace')
    client.engine.dispose()

    # make sales data for pg and sqlite
    loader = load_dotted_path(dag_spec['clients']['SQLiteRelation'])
    client = loader()
    df.to_sql('sales', client.engine)
    client.engine.dispose()

    dag = DAGSpec(dag_spec).to_dag()

    dag.build()


def test_pipeline_r(tmp_pipeline_r):
    Path('output').mkdir()

    with open('pipeline.yaml') as f:
        dag_spec = yaml.load(f, Loader=yaml.SafeLoader)

    dag = DAGSpec(dag_spec).to_dag()
    dag.build()


def test_initialize_with_lazy_import_with_missing_kernel(
        tmp_pipeline_r, monkeypatch):
    Path('output').mkdir()

    with open('pipeline.yaml') as f:
        dag_spec = yaml.load(f, Loader=yaml.SafeLoader)

    def no_kernel(name):
        raise ValueError

    monkeypatch.setattr(jupyter_client.kernelspec, 'get_kernel_spec',
                        no_kernel)

    assert DAGSpec(dag_spec, lazy_import=True).to_dag()


@pytest.mark.parametrize(
    'raw', [
        [
            {
                'source': 'load.py',
                'product': 'data.csv'
            },
        ],
        {
            'tasks': [{
                'source': 'load.py',
                'product': 'data.csv'
            }]
        },
        {
            'meta': {},
            'tasks': []
        },
    ],
    ids=['list-of-tasks', 'dict-with-tasks', 'dict-with-meta'])
def test_meta_defaults(raw):
    spec = DAGSpec(raw)
    meta = spec['meta']
    assert meta['extract_upstream']
    assert not meta['extract_product']
    assert not meta['product_relative_to_source']
    assert not meta['jupyter_hot_reload']


@pytest.mark.parametrize('name, value', [
    ['extract_upstream', False],
    ['extract_product', False],
    ['product_relative_to_source', True],
    ['jupyter_hot_reload', True],
])
def test_changing_defaults(name, value):
    spec = DAGSpec({'meta': {name: value}, 'tasks': []})
    assert spec['meta'][name] is value


@pytest.mark.parametrize('save', [True, False])
def test_expand_env(save, tmp_directory):
    env = {'sample': True, 'user': '{{user}}'}

    if save:
        with open('env.yaml', 'w') as f:
            yaml.dump(env, f)
        env = 'env.yaml'

    spec = DAGSpec(
        {
            'tasks': [{
                'source': 'plot.py',
                'product': 'output.ipynb',
                'params': {
                    'sample': '{{sample}}',
                    'user': '{{user}}'
                }
            }]
        },
        env=env)

    assert spec['tasks'][0]['params']['sample'] is True
    assert spec['tasks'][0]['params']['user'] == getpass.getuser()


def test_expand_built_in_placeholders(tmp_directory, monkeypatch):
    tmp_directory = Path(tmp_directory).resolve()
    Path('setup.py').touch()
    Path('src', 'pkg').mkdir(parents=True)
    Path('subdir').mkdir()

    def mockreturn():
        return 'username'

    monkeypatch.setattr(expand.getpass, "getuser", mockreturn)

    spec_dict = {
        'meta': {
            'extract_product': False
        },
        'tasks': [{
            'source': str(Path('{{root}}', 'script.py')),
            'product': {
                'nb': str(Path('{{cwd}}', '{{user}}', 'nb.html')),
                'data': str(Path('{{here}}', 'data.csv')),
            },
        }]
    }

    Path('src', 'pkg', 'pipeline.yaml').write_text(yaml.dump(spec_dict))

    os.chdir(Path('src', 'pkg'))

    spec = DAGSpec.find()

    assert spec.data['tasks'][0]['source'] == Path(tmp_directory, 'script.py')
    assert spec.data['tasks'][0]['product']['nb'] == str(
        Path(tmp_directory, 'src', 'pkg', 'username', 'nb.html'))
    assert spec.data['tasks'][0]['product']['data'] == str(
        Path(tmp_directory, 'src', 'pkg', 'data.csv'))


@pytest.mark.parametrize('method, kwargs', [
    [None, dict(data='pipeline.yaml')],
    ['_auto_load', dict(to_dag=False)],
])
def test_passing_env_in_class_methods(method, kwargs, tmp_directory):

    spec_dict = {
        'tasks': [{
            'source': 'plot.py',
            'product': 'output.ipynb',
            'params': {
                'some_param': '{{key}}',
            }
        }]
    }

    with open('pipeline.yaml', 'w') as f:
        yaml.dump(spec_dict, f)

    if method:
        callable_ = getattr(DAGSpec, method)
    else:
        callable_ = DAGSpec

    spec = callable_(**kwargs, env={'key': 'value'})

    # auto_load returns a tuple
    if isinstance(spec, tuple):
        spec = spec[0]

    assert spec['tasks'][0]['params']['some_param'] == 'value'


def test_infer_dependencies_sql(tmp_pipeline_sql, add_current_to_sys_path):
    expected = {'filter': {'load'}, 'transform': {'filter'}, 'load': set()}

    with open('pipeline-postgres.yaml') as f:
        d = yaml.safe_load(f)

    d['meta']['extract_upstream'] = True

    for t in d['tasks']:
        t.pop('upstream', None)

    dag = DAGSpec(d).to_dag()

    deps = {name: set(task.upstream) for name, task in dag.items()}
    assert deps == expected


# FIXME: fix this test on windows, there's something going on with the way
# Pathlib behaves on windows that makes the products comparison to fail
@pytest.mark.xfail(sys.platform == 'win32',
                   reason='Pathlib behaves differently on Windows')
def test_extract_variables_from_notebooks(tmp_nbs):
    with open('pipeline.yaml') as f:
        d = yaml.safe_load(f)

    d['meta']['extract_upstream'] = True
    d['meta']['extract_product'] = True

    for t in d['tasks']:
        t.pop('upstream', None)
        t.pop('product', None)

    dag = DAGSpec(d).to_dag()
    dependencies = {name: set(task.upstream) for name, task in dag.items()}
    products = {
        name: task.product.to_json_serializable()
        for name, task in dag.items()
    }

    expected_dependencies = {
        'clean': {'load'},
        'plot': {'clean'},
        'load': set()
    }

    expected_products = {
        'clean': {
            'data': str(Path(tmp_nbs, 'output', 'clean.csv').resolve()),
            'nb': str(Path(tmp_nbs, 'output', 'clean.ipynb').resolve()),
        },
        'load': {
            'data': str(Path(tmp_nbs, 'output', 'data.csv').resolve()),
            'nb': str(Path(tmp_nbs, 'output', 'load.ipynb').resolve()),
        },
        'plot': str(Path(tmp_nbs, 'output', 'plot.ipynb').resolve()),
    }

    assert dependencies == expected_dependencies
    assert products == expected_products


def test_source_loader(monkeypatch, tmp_directory, no_sys_modules_cache):
    monkeypatch.syspath_prepend(tmp_directory)

    spec = DAGSpec({
        'meta': {
            'source_loader': {
                'path': 'templates',
                'module': 'test_pkg'
            },
            'extract_product': False,
            'extract_upstream': False,
        },
        'tasks': [{
            'source': 'create-table.sql',
            'product': ['some_table', 'table'],
            'client': 'db.get_client'
        }]
    })

    Path('db.py').write_text("""
from ploomber.clients import SQLAlchemyClient

def get_client():
    return SQLAlchemyClient('sqlite://')
""")

    # check source loader is working correctly with a template that has a macro
    loader = spec['meta']['source_loader']
    template = loader['create-table.sql']

    expected = ('\nDROP TABLE IF EXISTS some_table;\nCREATE TABLE '
                'some_table AS\nSELECT * FROM table')
    assert template.render({'product': 'some_table'}) == expected

    # test the task source is correctly resolved when converted to a dag
    dag = spec.to_dag()
    dag.render()

    assert str(dag['create-table'].source) == expected


@pytest.mark.parametrize('lazy_import', [False, True])
def test_spec_with_functions(lazy_import, backup_spec_with_functions,
                             add_current_to_sys_path):
    """
    Check we can create pipeline where the task is a function defined in a
    local file
    """
    spec = DAGSpec('pipeline.yaml', lazy_import=lazy_import)
    spec.to_dag().build()


def test_spec_with_location(tmp_directory):
    Path('pipeline.yaml').write_text('location: some.factory.function')
    spec = DAGSpec('pipeline.yaml')
    assert spec['meta'] == {k: None for k in Meta.VALID}


def test_spec_with_location_error_if_meta(tmp_directory):
    Path('pipeline.yaml').write_text(
        'location: some.factory.function\nmeta: {some: key}')

    with pytest.raises(KeyError) as excinfo:
        DAGSpec('pipeline.yaml')

    assert ('If specifying dag through a "location" key it must be '
            'the unique key in the spec') in str(excinfo.value)


def test_to_dag_does_not_mutate_spec(tmp_nbs):
    spec = DAGSpec('pipeline.yaml')
    old_data = deepcopy(spec.data)
    spec.to_dag()
    assert spec.data == old_data


def test_import_tasks_from(tmp_nbs):
    some_tasks = [{'source': 'extra_task.py', 'product': 'extra.ipynb'}]
    Path('some_tasks.yaml').write_text(yaml.dump(some_tasks))
    Path('extra_task.py').write_text("""
# + tags=["parameters"]
# -
""")

    spec_d = yaml.safe_load(Path('pipeline.yaml').read_text())
    spec_d['meta']['import_tasks_from'] = 'some_tasks.yaml'

    spec = DAGSpec(spec_d)

    spec.to_dag().render()
    assert str(Path('extra_task.py').resolve()) in [
        str(t['source']) for t in spec['tasks']
    ]


def test_import_tasks_from_does_not_change_dotted_paths(tmp_nbs):
    some_tasks = [{
        'source': 'extra_task.py',
        'product': 'extra.ipynb'
    }, {
        'source': 'test_pkg.touch_root',
        'product': 'some_file.csv'
    }]
    Path('some_tasks.yaml').write_text(yaml.dump(some_tasks))

    spec_d = yaml.safe_load(Path('pipeline.yaml').read_text())
    spec_d['meta']['import_tasks_from'] = 'some_tasks.yaml'

    spec = DAGSpec(spec_d, lazy_import=True)
    assert 'test_pkg.touch_root' in [t['source'] for t in spec['tasks']]


def test_import_tasks_from_with_non_empty_env(tmp_nbs):
    some_tasks = [{
        'source': 'extra_task.py',
        'name': 'extra_task',
        'product': 'extra.ipynb',
        'params': {
            'some_param': '{{some_param}}'
        }
    }]
    Path('some_tasks.yaml').write_text(yaml.dump(some_tasks))
    Path('extra_task.py').write_text("""
# + tags=["parameters"]
# -
""")
    spec_d = yaml.safe_load(Path('pipeline.yaml').read_text())
    spec_d['meta']['import_tasks_from'] = 'some_tasks.yaml'

    spec = DAGSpec(spec_d, env={'some_param': 'some_value'})

    dag = spec.to_dag()
    dag.render()
    assert dag['extra_task'].params['some_param'] == 'some_value'
    assert str(Path('extra_task.py').resolve()) in [
        str(t['source']) for t in spec['tasks']
    ]


def test_import_tasks_from_loads_relative_to_pipeline_spec(tmp_nbs):
    some_tasks = [{'source': 'extra_task.py', 'product': 'extra.ipynb'}]
    Path('some_tasks.yaml').write_text(yaml.dump(some_tasks))
    Path('extra_task.py').write_text("""
# + tags=["parameters"]
# -
""")

    spec_d = yaml.safe_load(Path('pipeline.yaml').read_text())
    spec_d['meta']['import_tasks_from'] = 'some_tasks.yaml'

    Path('pipeline.yaml').write_text(yaml.dump(spec_d))

    # move to another dir to make sure we can still load the spec
    Path('subdir').mkdir()
    os.chdir('subdir')

    spec = DAGSpec('../pipeline.yaml')
    dag = spec.to_dag()
    dag.render()

    assert spec['meta']['import_tasks_from'] == str(
        Path('..', 'some_tasks.yaml').resolve())
    assert str(Path('..', 'extra_task.py').resolve()) in [
        str(t['source']) for t in spec['tasks']
    ]


def test_import_tasks_from_keeps_value_if_already_absolute(tmp_nbs, tmp_path):
    tasks_yaml = (tmp_path / 'some_tasks.yaml').resolve()
    path_to_script = (tmp_path / 'extra_task.py').resolve()

    some_tasks = [{'source': str(path_to_script), 'product': 'extra.ipynb'}]
    tasks_yaml.write_text(yaml.dump(some_tasks))
    path_to_script.write_text("""
# + tags=["parameters"]
# -
""")

    spec_d = yaml.safe_load(Path('pipeline.yaml').read_text())
    # set an absolute path
    spec_d['meta']['import_tasks_from'] = str(tasks_yaml)
    Path('pipeline.yaml').write_text(yaml.dump(spec_d))

    spec = DAGSpec('pipeline.yaml')
    dag = spec.to_dag()
    dag.render()

    # value should be the same because it was absolute
    assert spec['meta']['import_tasks_from'] == str(tasks_yaml)
    assert str(path_to_script) in [str(t['source']) for t in spec['tasks']]


def test_import_tasks_from_paths_are_relative_to_the_yaml_spec(
        tmp_nbs, tmp_path):
    tasks_yaml = tmp_path / 'some_tasks.yaml'

    # source is a relative path
    some_tasks = [{'source': 'extra_task.py', 'product': 'extra.ipynb'}]
    tasks_yaml.write_text(yaml.dump(some_tasks))
    #  write the source code in the same folder as some_tasks.yaml
    Path(tmp_path, 'extra_task.py').write_text("""
# + tags=["parameters"]
# -
""")

    spec_d = yaml.safe_load(Path('pipeline.yaml').read_text())

    # set an absolute path
    spec_d['meta']['import_tasks_from'] = str(tasks_yaml.resolve())
    Path('pipeline.yaml').write_text(yaml.dump(spec_d))

    spec = DAGSpec('pipeline.yaml')
    dag = spec.to_dag()
    dag.render()

    # paths must be interpreted as relative to tasks.yaml, not to the
    # current working directory
    assert str(Path(tmp_path, 'extra_task.py').resolve()) in [
        str(t['source']) for t in spec['tasks']
    ]


def test_loads_serializer_and_unserializer(backup_online,
                                           add_current_to_sys_path):

    spec = DAGSpec({
        'tasks': [{
            'source': 'online_tasks.get',
            'product': 'output/get.parquet',
        }, {
            'source': 'online_tasks.square',
            'product': 'output/square.parquet',
        }],
        'meta': {
            'extract_product': False
        },
        'serializer':
        'online_io.serialize',
        'unserializer':
        'online_io.unserialize',
    })

    dag = spec.to_dag()

    from online_io import serialize, unserialize

    assert dag['get']._serializer is serialize
    assert dag['get']._unserializer is unserialize
    assert dag['square']._serializer is serialize
    assert dag['square']._unserializer is unserialize


@pytest.mark.parametrize('root_path', ['.', 'subdir'])
def test_searches_in_default_locations(monkeypatch, tmp_nbs, root_path):
    root_path = Path(root_path).resolve()
    Path('subdir').mkdir()

    mock = Mock(wraps=dagspec.entry_point)
    monkeypatch.setattr(dagspec, 'entry_point', mock)

    DAGSpec._auto_load(starting_dir=root_path)

    mock.assert_called_once_with(root_path=root_path, name=None)


def test_find(tmp_nbs, monkeypatch):
    mock = Mock(return_value=[None, None])
    monkeypatch.setattr(dagspec.DAGSpec, '_auto_load', mock)

    env = {'a': 1}
    DAGSpec.find(env=env)

    mock.assert_called_once_with(to_dag=False,
                                 starting_dir=None,
                                 env={'a': 1},
                                 lazy_import=False,
                                 reload=False,
                                 name=None)


def test_error_invalid_yaml_displays_error_line(tmp_directory):
    Path('pipeline.yaml').write_text('key: [')

    with pytest.raises(yaml.parser.ParserError) as excinfo:
        DAGSpec('pipeline.yaml')

    assert 'key: [' in str(excinfo.value)


@pytest.mark.parametrize('content', [
    'key: {{placeholder}}/hello',
    'key: {{placeholder}}',
])
def test_error_invalid_yaml_with_placeholders_without_parentheses(
        tmp_directory, content):
    Path('pipeline.yaml').write_text(content)

    with pytest.raises(exceptions.DAGSpecInitializationError) as excinfo:
        DAGSpec('pipeline.yaml')

    assert content in str(excinfo.value)


@pytest.mark.parametrize(
    'code, expected_error',
    [[
        'get = 1', "Error loading dotted path 'dag_level_client_dotted_path"
        ".get'. Expected a callable object (i.e., some kind of function). "
        "Got 1 (an object of type: int)"
    ],
     [
         """
def get():
    return None
""", "Error calling dotted path "
         "'dag_level_client_dotted_path.get'. "
         "Expected a value but got None"
     ],
     [
         """
def get():
    return 42
""", 'client with value 42 does not have a split_source attribute. Make sure '
         'this is a valid client object (e.g., ploomber.clients.SQLALChemy '
         'or ploomber.clients.DBAPIClient)'
     ]],
    ids=['not-a-callable', 'returns-none', 'returns-not-a-client'])
def test_error_invalid_dag_level_client_dotted_path(tmp_sample_tasks,
                                                    add_current_to_sys_path,
                                                    no_sys_modules_cache, code,
                                                    expected_error):
    Path('dag_level_client_dotted_path.py').write_text(code)

    spec = DAGSpec({
        'meta': {
            'extract_product': False,
            'extract_upstream': True,
        },
        'tasks': [
            {
                'source': 'sample.sql',
                'product': ['name', 'table']
            },
        ],
        'clients': {
            'SQLScript': 'dag_level_client_dotted_path.get'
        }
    })

    with pytest.raises(TypeError) as excinfo:
        spec.to_dag()

    assert expected_error in str(excinfo.value)


@pytest.mark.parametrize(
    'dotted_path_spec',
    [
        'test_sets_clients.get',
        {
            'dotted_path': 'test_sets_clients.get'
        },
        {
            'dotted_path': 'test_sets_clients.get',
            'a': 1
        },
    ],
    ids=['str', 'dict', 'dict-with_kwargs'],
)
def test_sets_clients(tmp_sample_tasks, add_current_to_sys_path,
                      no_sys_modules_cache, dotted_path_spec):
    Path('test_sets_clients.py').write_text("""
from unittest.mock import Mock

def get(a=None):
    return Mock()
""")

    spec = DAGSpec({
        'meta': {
            'extract_product': False,
            'extract_upstream': True,
        },
        'tasks': [
            {
                'source': 'sample.sql',
                'product': ['name', 'table']
            },
        ],
        'clients': {
            'SQLScript': dotted_path_spec
        }
    })

    dag = spec.to_dag()

    assert isinstance(dag.clients[SQLScript], Mock)


def test_validate_product_default_class_keys():
    spec = {
        'meta': {
            'product_default_class': {
                'unknown_task': 'File',
            }
        },
        'tasks': [],
    }

    with pytest.raises(ValueError) as excinfo:
        DAGSpec(spec)

    expected = ("Error validating product_default_class: "
                "'unknown_task' is not a valid Task class name")
    assert str(excinfo.value) == expected


def test_validate_product_default_class_values():
    spec = {
        'meta': {
            'product_default_class': {
                'SQLDump': 'unknown_product',
            }
        },
        'tasks': [],
    }

    with pytest.raises(ValueError) as excinfo:
        DAGSpec(spec)

    expected = ("Error validating product_default_class: "
                "'unknown_product' is not a valid Product class name")
    assert str(excinfo.value) == expected


def test_warn_if_param_declared_in_env_but_unused_in_spec():

    with pytest.warns(UserWarning) as record:
        DAGSpec(
            {
                'tasks': [
                    {
                        'source': 'sample.sql',
                        'product': ['name', 'table']
                    },
                ]
            },
            env=dict(a=1))

    assert len(record) == 1
    expected = ("The following placeholders are declared in the "
                "environment but unused in the spec: {'a'}")
    assert str(record[0].message) == expected


def test_doesnt_warn_if_param_declared_in_env_is_used_in_spec():

    with pytest.warns(None) as record:
        DAGSpec(
            {
                'tasks': [
                    {
                        'source': 'sample.sql',
                        'product': ['{{a}}', 'table']
                    },
                ]
            },
            env=dict(a=1))

    assert not record


def test_doesnt_warn_if_param_is_used_in_import_task_from(tmp_directory):
    Path('more_tasks.yaml').write_text("""
- source: another.sql
  product: [name, table]
  params:
    a: '{{a}}'
""")

    with pytest.warns(None) as record:
        DAGSpec(
            {
                'meta': {
                    'import_tasks_from': 'more_tasks.yaml'
                },
                'tasks': [
                    {
                        'source': 'sample.sql',
                        'product': ['name', 'table']
                    },
                ]
            },
            env=dict(a=1))

    assert not record


_spec_upstream_extract = {
    'tasks': [{
        'source': 'upstream.py',
        'name': 'upstream-',
        'product': 'upstream.ipynb',
        'grid': {
            'param': [1, 2]
        }
    }, {
        'source': 'downstream.py',
        'product': 'downstream.ipynb'
    }]
}

_spec_upstream_manual = {
    'meta': {
        'extract_upstream': False
    },
    'tasks': [{
        'source': 'upstream.py',
        'name': 'upstream-',
        'product': 'upstream.ipynb',
        'grid': {
            'param': [1, 2]
        }
    }, {
        'source': 'downstream.py',
        'product': 'downstream.ipynb',
        'upstream': ['upstream-*'],
    }]
}


@pytest.mark.parametrize('spec',
                         [_spec_upstream_extract, _spec_upstream_manual])
def test_grid_and_upstream_wildcard_scripts(spec, tmp_path):
    Path('upstream.py').write_text("""
# + tags=['parameters']
upstream = None
""")

    Path('downstream.py').write_text("""
# + tags=['parameters']
upstream = ['upstream-*']
""")

    spec = DAGSpec(spec)

    dag = spec.to_dag().render()

    assert set(dag) == {'upstream-1', 'upstream-0', 'downstream'}

    assert set(dag['downstream'].params['upstream']['upstream-*']) == {
        'upstream-1', 'upstream-0'
    }


_spec_callables = {
    'tasks': [{
        'source': 'sample_source_callables.upstream',
        'name': 'upstream-',
        'product': 'upstream.txt',
        'grid': {
            'param': [1, 2]
        }
    }, {
        'source': 'sample_source_callables.downstream',
        'product': 'downstream.txt'
    }]
}

_spec_callables_unserializer = {
    'unserializer':
    'sample_source_callables.unserializer',
    'tasks': [{
        'source': 'sample_source_callables.upstream',
        'name': 'upstream-',
        'product': 'upstream.txt',
        'grid': {
            'param': [1, 2]
        }
    }, {
        'source': 'sample_source_callables.downstream',
        'product': 'downstream.txt'
    }]
}


@pytest.mark.parametrize('spec_raw',
                         [_spec_callables, _spec_callables_unserializer],
                         ids=['simple', 'with-unserializer'])
def test_grid_and_upstream_wildcard_callables(spec_raw, tmp_directory,
                                              add_current_to_sys_path,
                                              no_sys_modules_cache):
    Path('sample_source_callables.py').write_text("""
from pathlib import Path

def unserializer(product):
    return Path(product).read_text()

def upstream(product, param):
    Path(product).touch()

def downstream(product, upstream):
    up = upstream['upstream-*']
    one = up['upstream-0']
    another = up['upstream-1']
    Path(product).touch()
""")

    spec = DAGSpec(spec_raw)

    dag = spec.to_dag().render()
    # to build faster
    dag.executor = Serial(build_in_subprocess=False)

    # make sure unserializing works correctly
    dag.build()

    assert set(dag) == {'upstream-1', 'upstream-0', 'downstream'}
    assert set(dag['downstream'].params['upstream']['upstream-*']) == {
        'upstream-1', 'upstream-0'
    }


@pytest.mark.parametrize('spec',
                         [_spec_upstream_extract, _spec_upstream_manual])
def test_spec_from_yaml_resolves_paths_from_wildcard(tmp_directory, spec):
    Path('upstream.py').write_text("""
# + tags=['parameters']
upstream = None
""")

    Path('downstream.py').write_text("""
# + tags=['parameters']
upstream = ['upstream-*']
""")

    spec = DAGSpec(spec)

    dag = spec.to_dag().render()

    # on windows, paths do not resolve if the file doesn't exist
    Path('upstream-0.ipynb').touch()
    Path('upstream-1.ipynb').touch()

    assert str(Path(dag['upstream-0'].product).resolve()) == str(
        Path('upstream-0.ipynb').resolve())
    assert str(Path(dag['upstream-1'].product).resolve()) == str(
        Path('upstream-1.ipynb').resolve())


def test_load_spec_with_custom_name(tmp_nbs):
    shutil.copy('pipeline.yaml', 'pipeline.serve.yaml')
    spec = DAGSpec.find(name='serve')
    assert spec.path.resolve() == Path('pipeline.serve.yaml').resolve()


def test_load_spec_with_custom_name_in_packaged_structure(backup_test_pkg):
    os.chdir(Path(backup_test_pkg).parents[1])

    path = Path('src', 'test_pkg')
    shutil.copy(path / 'pipeline.yaml', path / 'pipeline.serve.yaml')

    spec = DAGSpec.find(name='serve')
    assert spec.path == (path / 'pipeline.serve.yaml').resolve()


@pytest.mark.parametrize('filename, name', [
    ['pipeline.yaml', None],
    ['pipeline.serve.yaml', 'serve'],
])
def test_load_spec_relative(tmp_nbs, filename, name):
    os.rename('pipeline.yaml', filename)
    spec = DAGSpec._find_relative(name=name)
    assert spec.path == Path(filename).resolve()


@pytest.mark.parametrize('filename, name', [
    ['pipeline.yaml', None],
    ['pipeline.serve.yaml', 'serve'],
])
def test_load_spec_relative_and_in_a_package(backup_test_pkg, filename, name):
    os.chdir(Path(backup_test_pkg).parents[1])

    path = Path('src', 'test_pkg')
    os.rename(path / 'pipeline.yaml', path / filename)

    spec = DAGSpec._find_relative(name=name)
    assert spec.path == Path('src', 'test_pkg', filename).resolve()