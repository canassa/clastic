from __future__ import unicode_literals

import inspect
import collections
from collections import defaultdict
import types

from werkzeug.wrappers import Request, Response
from werkzeug.routing import Map, Rule
from werkzeug.exceptions import HTTPException, NotFound
from werkzeug.wsgi import SharedDataMiddleware
from werkzeug.utils import redirect, cached_property

from werkzeug.routing import parse_rule

from decorator import decorator

# TODO: 'next' is really only reserved for middlewares
RESERVED_ARGS = ('request', 'next', 'context', '_application', '_route', '_endpoint')

def getargspec(f):
    ret = inspect.getargspec(f)
    if not all([isinstance(a, basestring) for a in ret.args]):
        raise TypeError('does not support anonymous tuple arguments '
                        'or any other strange args for that matter.')
    if isinstance(f, types.MethodType):
        ret = ret._replace(args=ret.args[1:])  # throw away "self"
    return ret


def get_arg_names(f, only_required=False):
    arg_names, _, _, defaults = getargspec(f)

    if only_required and defaults:
        arg_names = arg_names[:-len(defaults)]

    return tuple(arg_names)


def inject(f, injectables):
    arg_names, _, _, defaults = inspect.getargspec(f)
    if defaults:
        defaults = dict(reversed(zip(reversed(arg_names), reversed(defaults))))
    else:
        defaults = {}
    if isinstance(f, types.MethodType):
        arg_names = arg_names[1:] #throw away "self"
    args = {}
    for n in arg_names:
        if n in injectables:
            args[n] = injectables[n]
        else:
            args[n] = defaults[n]
    return f(**args)


class Application(Map):
    def __init__(self, routes=None, resources=None, render_factory=None,
                 middlewares=None, **map_kwargs):
        map_kwargs.pop('rules', None)
        super(Application, self).__init__(**map_kwargs)

        self.routes = []
        self.resources = dict(resources or {})
        self.middlewares = list(middlewares or [])
        self.render_factory = render_factory
        self.endpoint_args = {}
        self._map_kwargs = map_kwargs
        for entry in routes:
            rule_factory = Route.cast(entry)
            for r in rule_factory.get_rules(self):
                self.add_route(r)

    def add_route(self, route):
        # note: currently only works with individual routes
        nr = route.copy()  # is copy necessary here?
        self.add(nr)
        return nr

    @property
    def injectable_names(self):
        return set(self.resources.keys() + RESERVED_ARGS)

    def get_rules(self, r_map=None):
        if r_map is None:
            r_map = self
        for rf in self.routes:
            for rule in rf.get_rules(r_map):
                yield rule  # is yielding bound rules bad?

    def match(self, request):
        adapter = self.bind_to_environ(request.environ)
        route, values = adapter.match(return_rule=True)
        request.path_params = values
        injectables = dict(self.resources)
        injectables['request'] = request
        injectables['req'] = request
        injectables['application'] = self
        injectables.update(values)
        ep_arg_names = route.endpoint_args
        ep_kwargs = dict([(k, v) for k, v in injectables.items()
                          if k in ep_arg_names])
        return route, ep_kwargs

    def respond(self, request):
        try:
            route, ep_kwargs = self.match(request)
            ep_res = route.execute(request, **ep_kwargs)
        except (HTTPException, NotFound) as e:
            return e

        if isinstance(ep_res, Response):
            return ep_res
        elif callable(getattr(route, 'render', None)):
            return route.render(ep_res)
        else:
            #import pdb;pdb.set_trace()
            return HTTPException('no renderer registered for ' + repr(route) + \
                                 ' and no Response returned')
        #TODO: default renderer?

    def __call__(self, environ, start_response):
        request = Request(environ)
        response = self.respond(request)
        return response(environ, start_response)


class Middleware(object):
    unique = True
    reorderable = True
    provides = ()
    endpoint_provides = ()

    request = None
    endpoint = None
    render = None

    @property
    def name(self):
        return self.__class__.__name__

    @property
    def overridable(self):
        # thought: list of overridable provides?
        return tuple(self.provides)

    def __eq__(self, other):
        return type(self) == type(other)

    def __ne__(self, other):
        return type(self) != type(other)

    @cached_property
    def requirements(self):
        reqs = []
        if self.request:
            reqs.extend(get_arg_names(self.request, True))
        if self.endpoint:
            reqs.extend(get_arg_names(self.endpoint, True))
        if self.render:
            reqs.extend(get_arg_names(self.render, True))
        return set(reqs)

    @cached_property
    def arguments(self):
        args = []
        if self.request:
            args.extend(get_arg_names(self.request))
        if self.endpoint:
            args.extend(get_arg_names(self.endpoint))
        if self.render:
            args.extend(get_arg_names(self.render))
        return set(args)


class DummyMiddleware(Middleware):
    def __init__(self):
        pass

    def request(self, next, request):
        print self, 'handling', id(request)
        try:
            ret = next()
        except:
            print self, 'uhoh'
            raise
        print self, 'hooray'
        return ret

def check_middleware(mw):
    for f_name in ('request', 'endpoint', 'render'):
        func = getattr(mw, f_name, None)
        if not func:
            continue
        if not callable(func):
            raise TypeError('expected middleware.'+f_name+' to be a function')
        if not get_arg_names(func)[0] == 'next':
            raise TypeError("middleware functions must take a first parameter 'next'")

def check_middlewares(middlewares, args_dict=None):
    args_dict = args_dict or {}

    provided_by = defaultdict(list)
    for source, arg_list in args_dict.items():
        for arg_name in arg_list:
            provided_by[arg_name].append(source)

    for mw in middlewares:
        check_middleware(mw)
        for arg in mw.provides:
            provided_by[arg].append(mw)

    conflicts = [(n, tuple(ps)) for (n, ps) in provided_by.items() if len(ps) > 1]
    if conflicts:
        raise ValueError('route argument conflicts: '+repr(conflicts))
    return True


def merge_middlewares(old, new):
    # TODO: since duplicate provides aren't allowed
    # an error needs to be raised if a middleware is
    # set to non-unique and has provides params

    old = list(old)
    merged = list(new)
    for mw in old:
        if mw.unique and mw in merged:
            if mw.reorderable:
                continue
            else:
                raise ValueError('multiple inclusion of unique '
                                 'middleware '+mw.name)
        merged.append(mw)
    return merged


class Route(Rule):
    def __init__(self, rule_str, endpoint, render_arg=None, *a, **kw):
        super(Route, self).__init__(rule_str, *a, endpoint=endpoint, **kw)
        self._middlewares = []
        self._resources = {}
        self._reqs = None  # TODO
        self._args = None
        self._bound_apps = []
        self.endpoint_args = get_arg_names(endpoint)
        self.endpoint_reqs = get_arg_names(endpoint, True)

        self._render = None
        self.render_arg = render_arg
        if callable(render_arg):
            self._render = render_arg

    @property
    def is_bound(self):
        return self.map is not None

    @property
    def render(self):
        return self._render

    def empty(self):
        ret = Route(self.rule, self.endpoint, self.render_arg)
        ret.__dict__.update(super(Route, self).empty().__dict__)
        return ret

    def copy(self):
        # todo: there's probably more
        ret = self.empty()
        ret._render = self.render
        ret._middlewares = list(self._middlewares)
        return ret

    def bind(self, app):
        resources = app.__dict__.get('resources', {})
        render_factory = app.__dict__.get('render_factory')
        middlewares = app.__dict__.get('middlewares', [])

        merged_resources = self._resources.copy()
        merged_resources.update(resources)
        merged_mw = merge_middlewares(self._middlewares, middlewares)

        r_copy = self.copy()
        try:
            r_copy._bind_args(app, merged_resources, merged_mw, render_factory)
        except:
            raise

        self._bind_args(app, merged_resources, middlewares, render_factory)
        self._bound_apps.append(app)
        return self

    def _bind_args(self, url_map, resources, middlewares, render_factory):
        super(Route, self).bind(url_map, rebind=True)
        url_args = set(self.arguments)
        builtin_args = set(RESERVED_ARGS)
        resource_args = set(resources.keys())

        tmp_avail_args = {'url':url_args,
                          'builtins':builtin_args,
                          'resources': resource_args}
        check_middlewares(middlewares, tmp_avail_args)
        provided = resource_args | builtin_args | url_args
        if callable(render_factory) and self.render_arg is not None:
            _render = render_factory(self.render_arg)
        else:
            _render = lambda context: context
        _execute = make_middleware_chain(middlewares, self.endpoint, _render, provided)

        self._resources.update(resources)
        self._render = _render
        self._execute = _execute

    def execute(self, request, **kwargs):  # , resources=None):
        injectables = {
                       'request': request,
                       '_application': self._bound_apps[-1],
                       '_endpoint': self.endpoint,
                       '_route': self}
        injectables.update(self._resources)
        injectables.update(kwargs)
        return inject(self._execute, injectables)

    @classmethod
    def cast(cls, in_arg):
        if isinstance(in_arg, cls):
            return in_arg
        elif isinstance(in_arg, Rule):
            ret = cls(in_arg.rule, in_arg.endpoint)
            ret.__dict__.update(in_arg.empty().__dict__)
            return ret
        elif isinstance(in_arg, collections.Sequence):
            try:
                return cls(*in_arg)
            except TypeError:
                pass
        elif isinstance(in_arg, Application):
            pass
        raise TypeError('incompatible Route type: ' + repr(in_arg))


# GET/POST param middleware factory
# ordered sets?

# should resource values be bound into the route,
# or just check argument names and let the application do the
# resource merging?


# exec req middlewares
# -> exec endpoint middlewares
#  -> endpoint (*get context or response)
# -> ret endpoint middlewares
# -> exec render middlewares (if not response)
#  -> render (*get response)
# -> ret render middlewares
# ret req middlewares

"""
        endpoint_args = set(self.endpoint_args)
        endpoint_reqs = set(self.endpoint_reqs)

        route_reqs = set(endpoint_reqs)
        route_args = set(endpoint_args)

        mw_unresolved = defaultdict(list)
        for mw in spec_mw:
            route_reqs.update(mw.requirements)
            route_args.update(mw.arguments)
            cur_unresolved = mw.requirements - provided
            if cur_unresolved:
                mw_unresolved[mw] = tuple(cur_unresolved)
            provided.update(set(mw.provides))
        if mw_unresolved:
            raise ValueError('unresolved middleware arguments: '+repr(dict(mw_unresolved)))

        ep_unresolved = endpoint_reqs - provided
        if ep_unresolved:
            raise ValueError('unresolved endpoint arguments: '+repr(tuple(ep_unresolved)))

        self._reqs = route_reqs - set(['next'])
        self._args = route_args - set(['next'])  # Route signature (TODO: defaults)
"""



def chain_argspec(func_list, provides):
    provided_sofar = set(['next']) #'next' is an extremely special case
    optional_sofar = set()
    required_sofar = set()
    for f, p in zip(func_list, provides):
        # middlewares can default the same parameter to different values;
        # can't properly keep track of default values
        arg_names, _, _, defaults = getargspec(f)

        def_offs = -len(defaults) if defaults else None
        undefaulted, defaulted = arg_names[:def_offs], arg_names[def_offs:]
        optional_sofar.update(defaulted)
        # keep track of defaults so that e.g. endpoint default param can pick up
        # request injected/provided param
        required_sofar |= set(undefaulted) - provided_sofar
        provided_sofar.update(p)

    return required_sofar, optional_sofar

#NOTE: checking for double provided variables is assumed to already be done
#NOTE: any "hoisting" of middlewares / removing of duplicates should be done before this function
def make_middleware_chain(middlewares, endpoint, render, provided):
    endpoints = [(mw.endpoint, mw.endpoint_provides)
                    for mw in middlewares if mw.endpoint]
    renders = [mw.render for mw in middlewares if mw.render]
    requests = [(mw.request, mw.provides) for mw in middlewares if mw.request]

    #maybe there aren't and endpoints or requests functions defined at all
    if endpoints:
        endpoints, endpoints_provides = zip(*endpoints)
    else:
        endpoints_provides = []
    if requests:
        requests, requests_provides = zip(*requests)
    else:
        requests_provides = []

    request_params, request_optional =\
        chain_argspec(requests, requests_provides)

    endpoint_params, endpoint_optional =\
        chain_argspec(list(endpoints)+[endpoint], list(endpoints_provides)+[()])

    renders_params, renders_optional =\
        chain_argspec(list(renders)+[render], [('context',)]*(len(middlewares)+1))

    available_params = set(sum(map(tuple, requests_provides), ()) + tuple(provided))

    if endpoint_params - available_params:
        raise Exception("unresolved endpoint resources")

    if request_params - set(provided):
        raise Exception("unresolved request resources")

    if renders_params - available_params - set(['context']):
        raise Exception("unresolved renders resources")

    #add provided optional parameters back into actual parameters
    endpoint_params |= available_params & endpoint_optional
    renders_params  |= available_params & renders_optional
    tmp_ep_name = endpoint.__name__
    endpoint = make_chain(
        list(endpoints)+[endpoint],
        [endpoint_params]+[mw.endpoint_provides for mw in middlewares])

    render = make_chain(
        list(renders)+[render],
        [renders_params]+[('context',)]*len(middlewares))

    def named_arg_str(args): return ','.join([a+'='+a for a in args])

    inner_code = \
    'def inner('+','.join(endpoint_params|renders_params-set(['context']))+'):\n'+\
    '   context = endpoint('+named_arg_str(endpoint_params)+')\n'+\
    '   resp = render('+named_arg_str(renders_params)+')\n'+\
    '   return resp'

    d = {'endpoint':endpoint, 'render':render}
    exec compile(inner_code, '<string>', 'single') in d

    if tmp_ep_name.startswith('get_stops'):
        import pdb;pdb.set_trace()

    mw_exec = make_chain(
        list(requests)+[d['inner']], [request_params]+list(requests_provides) )

    return mw_exec


def make_chain(funcs, params, verbose=True):
    call_str = make_call_str(funcs, params)
    code = compile(call_str, '<string>', 'single')
    if verbose:
        print call_str
    d = {'funcs':funcs}
    exec code in d
    return d['next']


#funcs[0] = function to call
#params[0] = parameters to take
def make_call_str(funcs, params, params_sofar=None, level=0):
    if not funcs:
        return '' #stopping case
    if params_sofar is None:
        params_sofar = set(['next'])
    params_sofar.update(params[0])
    next_args = inspect.getargspec(funcs[0])[0]
    if isinstance(funcs[0], types.MethodType):
        next_args = next_args[1:]
    next_args = ','.join([a+'='+a for a in next_args if a in params_sofar])
    return '   '*level +'def next('+','.join(params[0])+'):\n'+\
        make_call_str(funcs[1:], params[1:], params_sofar, level+1)+\
        '   '*(level+1)+'return funcs['+str(level)+']('+next_args+')\n'
