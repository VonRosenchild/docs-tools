import datetime
import itertools
import os
import pkg_resources
import sys

from fabric.api import cd, local, task, env, hide, settings
from fabric.utils import puts
from multiprocessing import cpu_count

from utils import ingest_yaml, expand_tree, swap_streams
from clean import cleaner
from make import runner
import generate
import process
import docs_meta

conf = docs_meta.get_conf()
paths = conf.build.paths

from intersphinx import intersphinx, intersphinx_jobs
intersphinx = task(intersphinx)

env.EDITION = None
@task
def edition(val=None):
    # this is a wrapper so we can use edition_setup elsewhere
    edition_setup(val, conf)

def edition_setup(val, conf):
    if val is None and env.EDITION is not None:
        val = env.EDITION

    if 'editions' in conf.project and val in conf.project.editions:
        env.EDITION = val
        conf.project.edition = val

    if conf.project.name == 'mms':
        conf.build.paths.public_site_output = conf.build.paths.mms[val]

        if val == 'saas':
            conf.build.paths.branch_output = os.path.join(conf.build.paths.output, val)
        elif val == 'hosted':
            conf.build.paths.branch_output = os.path.join(conf.build.paths.output, val,
                                                           conf.git.branches.current)
    return conf

def get_tags(target, argtag):
    if argtag is None:
        ret = []
    else:
        ret = [argtag]

    if target.startswith('html') or target.startswith('dirhtml'):
        ret.append('website')
    else:
        ret.append('print')

    return ' '.join([ '-t ' + i for i in ret ])

def timestamp(form='filename'):
    if form == 'filename':
        return datetime.datetime.now().strftime("%Y-%m-%d.%H-%M")
    else:
        return datetime.datetime.now().strftime("%Y-%m-%d, %H:%M %p")

def get_sphinx_args(tag):
    o = ''

    if pkg_resources.get_distribution("sphinx").version.startswith('1.2b1-xgen') and (tag is None or not tag.startswith('hosted') or not tag.startswith('saas')):
         o += '-j ' + str(cpu_count() + 1) + ' '

    return o

#################### Associated Sphinx Artifacts ####################

def html_tarball():
    process.copy_if_needed(os.path.join(conf.build.paths.projectroot,
                                        conf.build.paths.includes, 'hash.rst'),
                           os.path.join(conf.build.paths.projectroot,
                                        conf.build.paths.branch_output,
                                        'html', 'release.txt'))

    basename = os.path.join(conf.build.paths.projectroot,
                            conf.build.paths.public_site_output,
                            conf.project.name + '-' + conf.git.branches.current)

    tarball_name = basename + '.tar.gz'

    generate.tarball(name=tarball_name,
                     path='html',
                     cdir=os.path.join(conf.build.paths.projectroot,
                                       conf.build.paths.branch_output),
                     sourcep='html',
                     newp=os.path.basename(basename))

    process._create_link(input_fn=os.path.basename(tarball_name),
                         output_fn=os.path.join(conf.build.paths.projectroot,
                                                conf.build.paths.public_site_output,
                                                conf.project.name + '.tar.gz'))

def man_tarball():
    basename = os.path.join(conf.build.paths.projectroot,
                            conf.build.paths.branch_output,
                            'manpages-' + conf.git.branches.current)

    tarball_name = basename + '.tar.gz'
    generate.tarball(name=tarball_name,
                     path='man',
                     cdir=os.path.dirname(basename),
                     sourcep='man',
                     newp=conf.project.name + '-manpages'
                     )

    process.copy_if_needed(tarball_name,
                           os.path.join(conf.build.paths.projectroot,
                                        conf.build.paths.public_site_output,
                                        os.path.basename(tarball_name)))

    process._create_link(input_fn=os.path.basename(tarball_name),
                         output_fn=os.path.join(conf.build.paths.projectroot,
                                                conf.build.paths.public_site_output,
                                                'manpages' + '.tar.gz'))

#################### Public Fabric Tasks ####################

## modifiers

@task
def prereq():
    jobs = itertools.chain(process.manpage_jobs(),
                           generate.table_jobs(),
                           generate.api_jobs(conf),
                           generate.toc_jobs(),
                           generate.steps_jobs(),
                           generate.release_jobs(),
                           intersphinx_jobs(),
                           generate.image_jobs())

    job_count = runner(jobs)
    dep_count = runner(process.composite_jobs())
    puts('[sphinx-prep]: built {0} pieces of content'.format(job_count))
    puts('[sphinx-prep]: checked timestamps of all {0} files'.format(dep_count))
    generate.buildinfo_hash()
    generate.source()
    puts('[sphinx-prep]: build environment prepared for sphinx.')

@task
def build(builder='html', tag=None, root=None):
    if root is None:
        root = conf.build.paths.branch_output

    with settings(host_string='sphinx'):
        dirpath = os.path.join(root, builder)
        if not os.path.exists(dirpath):
            os.makedirs(dirpath)
            puts('[{0}]: created {1}/{2}'.format(builder, root, builder))

        puts('[{0}]: starting {0} build {1}'.format(builder, timestamp()))

        cmd = 'sphinx-build -b {0} {1} -q -d {2}/doctrees-{0} -c {3} {4} {2}/source {2}/{0}' # per-builder-doctreea
        sphinx_cmd = cmd.format(builder, get_tags(builder, tag), root, conf.build.paths.projectroot, get_sphinx_args(tag))

        out = local(sphinx_cmd, capture=True)
        # out = build_sphinx_native(sphinx_cmd)

        with settings(host_string=''):
            output_sphinx_stream(out, builder, conf)

        puts('[build]: completed {0} build at {1}'.format(builder, timestamp()))

        finalize_build(builder, conf, root)

def build_sphinx_native(sphinx_cmd):
    # Calls sphinx directly rather than in a subprocess/shell. Not used
    # currently because of the effect on subsequent multiprocessing pools.

    try:
        from cStringIO import StringIO
    except ImportError:
        from StringIO import StringIO

    sp_cmd = __import__('sphinx.cmdline')

    sphinx_argv = sphinx_cmd.split()

    with swap_streams(StringIO()) as _out:
        r = sp_cmd.main(argv=sphinx_argv)
        out = _out.getvalue()

    if r != 0:
        exit(r)
    else: 
        return r

def output_sphinx_stream(out, builder, conf=None):
    if conf is None:
        conf = get_conf()

    out = out.split('\n')
    out = list(set(out))

    for l in out:
        if l == '':
            continue

        if builder.startswith('epub'):
            if l.startswith('WARNING: unknown mimetype'):
                continue
            elif len(l) == 0:
                continue
            elif l.startswith('WARNING: search index'):
                continue

        full_path = os.path.join(conf.build.paths.projectroot, conf.build.paths.branch_output)
        if l.startswith(conf.build.paths.branch_output):
            l = l[len(conf.build.paths.branch_output)+1:]
        elif l.startswith(full_path):
            l = l[len(full_path)+1:]

        print(l)

def finalize_build(builder, conf, root):
    pjoin = os.path.join
    single_html_dir = pjoin(conf.build.paths.public_site_output, 'single')

    if not os.path.exists(single_html_dir):
        os.makedirs(single_html_dir)

    if builder.startswith('linkcheck'):
        puts('[{0}]: See {1}/{0}/output.txt for output.'.format(builder, root))
    elif builder.startswith('dirhtml'):
        process.error_pages()
        process.copy_if_needed(source_file=pjoin(conf.build.paths.branch_output,
                                                 'dirhtml', 'index.html'),
                               target_file=pjoin(single_html_dir, 'search.html'))
    elif builder.startswith('json'):
        count = runner( process.json_output_jobs(conf) )
        puts('[json]: processed {0} json files.'.format(count - 1))
        process.json_output(conf)
    elif builder.startswith('singlehtml'):
        runner( finalize_single_html_jobs(conf, single_html_dir) )
    elif builder.startswith('latex'):
        process.pdfs()
    elif builder.startswith('man'):
        runner( process.manpage_url_jobs() )
        man_tarball()
    elif builder.startswith('html'):
        html_tarball()

def finalize_single_html_jobs(conf, single_html_dir):
    pjoin = os.path.join

    try:
        process.manual_single_html(input_file=pjoin(conf.build.paths.branch_output,
                                                    'singlehtml', 'contents.html'),
                                   output_file=pjoin(single_html_dir, 'index.html'))
    except (IOError, OSError):
        process.manual_single_html(input_file=pjoin(conf.build.paths.branch_output,
                                                    'singlehtml', 'index.html'),
                                   output_file=pjoin(single_html_dir, 'index.html'))
    process.copy_if_needed(source_file=pjoin(conf.build.paths.branch_output,
                                             'singlehtml', 'objects.inv'),
                           target_file=pjoin(single_html_dir, 'objects.inv'))

    single_path = pjoin(single_html_dir, '_static')

    for fn in expand_tree(pjoin(conf.build.paths.branch_output,
                                'singlehtml', '_static'), None):

        yield {
            'job': process.copy_if_needed,
            'args': [fn, pjoin(single_path, os.path.basename(fn))],
            'target': None,
            'dependency': None
        }

