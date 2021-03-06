# -*- coding: utf-8 -*-
"""
    libs.hook
    ~~~~~~~~~

    As a proxy to dynamically load and unload hook methods.

    :copyright: (c) 2019 by staugur.
    :license: BSD 3-Clause, see LICENSE for more details.
"""

from time import time
from sys import modules
from os import listdir, getpid
from os.path import join, dirname, abspath, isdir, isfile, splitext, basename,\
    getmtime
from jinja2 import ChoiceLoader, FileSystemLoader, PackageLoader
from flask import render_template, render_template_string, Markup
from utils.tool import Attribution, logger
from utils._compat import string_types, integer_types, iteritems, text_type, \
    PY2
from config import GLOBAL
from .storage import get_storage


class HookManager(object):

    def __init__(
        self,
        app=None,
        hooks_dir="hooks",
        reload_time=600,
        third_hooks=[]
    ):
        """Receive initialization parameters and
        pass options to :meth:`init_app` method.
        """
        self.__storage = get_storage()
        self.__hf = hooks_dir
        self.__hooksdir = join(dirname(dirname(abspath(__file__))), self.__hf)
        self.__MAX_RELOAD_TIME = int(GLOBAL["HookReloadTime"] or reload_time)
        self.__third_hooks = third_hooks
        self.__last_load_time = time()
        #: local and thirds hooks data
        self.__hooks = {}
        #: Initialize app via a factory
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        self.__init_load_hooks()
        #: Register template variable
        app.jinja_env.globals.update(
            intpl=self.call_intpl,
            get_call_list=self.get_call_list,
        )
        #: Custom add multiple template folders.
        app.jinja_loader = ChoiceLoader([
            app.jinja_loader,
            FileSystemLoader(self.__get_valid_tpl),
        ])
        #: register extension with app
        app.extensions = getattr(app, 'extensions', None) or {}
        app.extensions['hookmanager'] = self
        self.app = app

    @property
    def __last_load_time(self):
        hlt = self.__storage.get("hookloadtime") or {}
        return hlt.get(getpid())

    @__last_load_time.setter
    def __last_load_time(self, timestamp):
        if not isinstance(timestamp, (integer_types, float)):
            raise TypeError("The value of last_load_time type error")
        hlt = self.__storage.get("hookloadtime") or {}
        if timestamp == 0:
            hlt = {k: 0 for k, v in iteritems(hlt)}
        else:
            hlt[getpid()] = timestamp
        self.__storage.set("hookloadtime", hlt)

    @__last_load_time.deleter
    def __last_load_time(self):
        if "hookloadtime" in self.__storage.list:
            del self.__storage['hookloadtime']

    def __del__(self):
        del self.__last_load_time

    def __ensure_reloaded(self):
        hlt = self.__storage.get("hookloadtime") or {}
        hlt = {int(k): v for k, v in iteritems(hlt)}
        myself = hlt.get(getpid(), 0)
        if 0 in hlt.values() or (time() - myself) > self.__MAX_RELOAD_TIME:
            self.__hooks = {}
            self.__last_load_time = time()

    @property
    def __third_hooks(self):
        return self.__storage.get("hookthirds") or []

    @__third_hooks.setter
    def __third_hooks(self, third_hook_name):
        if not third_hook_name:
            return
        hooks = set(self.__storage.get("hookthirds") or [])
        if isinstance(third_hook_name, string_types):
            if third_hook_name.endswith(":delete"):
                delete_name = third_hook_name.split(":")[0]
                if delete_name in hooks:
                    hooks.remove(delete_name)
            else:
                hooks.add(third_hook_name)
        elif isinstance(third_hook_name, (list, tuple)):
            hooks.update(third_hook_name)
        self.__storage.set("hookthirds", list(set(hooks)))

    def __get_state_storage(self, name):
        s = set(self.__storage.get("hookstate") or [])
        d = "DISABLED.%s" % name
        e = "ENABLED.%s" % name
        if e in s:
            return 'enabled'
        elif d in s:
            return 'disabled'
        else:
            return None

    def __get_state(self, h):
        s = self.__get_state_storage(h.name)
        if s is None:
            s = h.state
        return s

    def __set_state_storage(self, name, state):
        s = set(self.__storage.get("hookstate") or [])
        d = "DISABLED.%s" % name
        e = "ENABLED.%s" % name
        if state == "disabled":
            if e in s:
                s.remove(e)
            s.add(d)
        elif state == "enabled":
            if d in s:
                s.remove(d)
            s.add(e)
        self.__storage.set("hookstate", list(s))

    def __get_fileorparent(self, obj, ask_dir=False):
        py = abspath(obj.__file__.replace(".pyc", ".py"))
        return dirname(py) if ask_dir else py

    def __init_load_hooks(self):
        self.__scan_local()
        self.__scan_third()

    def __scan_local(self):
        if isdir(self.__hooksdir):
            for f in listdir(self.__hooksdir):
                fn, fs = splitext(basename(f))
                fa, fm = join(self.__hooksdir, f), "%s.%s" % (self.__hf, fn)
                if isfile(fa) and fn != "__init__" and fs == ".py":
                    if fm in modules:
                        #: The mtime timestamp of the file when the module
                        #: was first imported.
                        if getattr(modules[fm], '__mtime__', 0) < getmtime(fa):
                            del modules[fm]
                    try:
                        fo = __import__(fm, fromlist=[self.__hooksdir])
                    except ImportError as e:
                        logger.error(e, exc_info=True)
                        continue
                    if hasattr(fo, "__version__") and \
                            hasattr(fo, "__author__"):
                        fo.__mtime__ = getmtime(fa)
                        fo.__family__ = "local"
                        self.__hooks[fm] = self.__get_meta(fo)

    def __scan_third(self):
        if self.__third_hooks and isinstance(self.__third_hooks, list):
            for hn in self.__third_hooks:
                if hn in modules:
                    hm = modules[hn]
                    if getattr(hm, '__mtime__', 0) < getmtime(
                        self.__get_fileorparent(hm)
                    ):
                        del hm
                try:
                    ho = __import__(hn)
                except ImportError as e:
                    logger.error(e, exc_info=True)
                    continue
                else:
                    if hasattr(ho, "__version__") and \
                            hasattr(ho, "__author__"):
                        ho.__mtime__ = getmtime(self.__get_fileorparent(ho))
                        ho.__family__ = "third"
                        self.__hooks[hn] = self.__get_meta(ho)

    def __get_meta(self, f_obj):
        name = getattr(
            f_obj, "__hookname__", f_obj.__name__.split('.')[-1],
        )
        state = self.__get_state_storage(name)
        if state is None:
            state = getattr(f_obj, "__state__", "enabled")
        return Attribution({
            "author": f_obj.__author__,
            "version": f_obj.__version__,
            "description": getattr(f_obj, "__description__", None),
            "state": state,
            "name": name,
            "proxy": f_obj,
            "time": time(),
            "catalog": getattr(f_obj, "__catalog__", None),
            "tplpath": join(self.__get_fileorparent(f_obj, True), "templates"),
        })

    @property
    def __get_valid_tpl(self):
        return [
            h.tplpath
            for h in self.get_all_hooks
            if isdir(h.tplpath)
        ]

    @property
    def get_all_hooks(self):
        """Get all hooks, enabled and disabled, returns list"""
        self.__ensure_reloaded()
        if not self.__hooks:
            self.__init_load_hooks()
        hooks = []
        for h in list(self.__hooks.values()):
            h['state'] = self.__get_state(h)
            hooks.append(h)
        return hooks

    @property
    def get_map_hooks(self):
        """Get all hooks, enabled and disabled, returns dict"""
        return {h.name: h for h in self.get_all_hooks}

    @property
    def get_enabled_hooks(self):
        """Get all enabled hooks, return list"""
        return [
            h
            for h in self.get_all_hooks
            if h.state == 'enabled'
        ]

    @property
    def get_enabled_map_hooks(self):
        """Get map enabled hooks, return dict"""
        return {
            name: h
            for name, h in iteritems(self.get_map_hooks)
            if h.state == 'enabled'
        }

    def disable(self, name):
        if name in self.get_map_hooks:
            self.__set_state_storage(name, "disabled")

    def enable(self, name):
        if name in self.get_map_hooks:
            self.__set_state_storage(name, "enabled")

    def reload(self):
        self.__hooks = {}
        self.__last_load_time = 0
        self.__init_load_hooks()

    def add_third_hook(self, third_hook_name):
        if third_hook_name:
            self.__third_hooks = third_hook_name
            if hasattr(self, 'app'):
                self.app.jinja_loader.loaders.append(
                    PackageLoader(third_hook_name)
                )
            self.reload()

    def remove_third_hook(self, third_hook_name):
        if third_hook_name:
            self.__third_hooks = "%s:delete" % third_hook_name
            self.reload()

    def proxy(self, name, is_enabled=True):
        """代理到钩子中执行方法

        :param is_enabled: 布尔值，True表示仅从已启用钩子中查找方法，否则查找所有
        """
        if is_enabled:
            if name in self.get_enabled_map_hooks:
                return self.get_enabled_map_hooks[name]["proxy"]
        else:
            if name in self.get_map_hooks:
                return self.get_map_hooks[name]["proxy"]

    def get_call_list(
        self, _callname, _include=None, _exclude=None, _type='all'
    ):
        hooks = []
        for h in sorted(self.get_enabled_hooks, key=lambda h: h.name):
            if _include and isinstance(_include, (tuple, list)):
                if h.name not in _include:
                    continue
            if _exclude and isinstance(_exclude, (tuple, list)):
                if h.name in _exclude:
                    continue
            hin = False
            tpl = getattr(h.proxy, "intpl_%s" % _callname, None)
            cn = getattr(h.proxy, _callname, None)
            if _type == "func":
                if callable(cn):
                    hin = True
            elif _type == "tpl":
                if tpl:
                    hin = True
            elif _type == "bool":
                if cn is True:
                    hin = True
            else:
                if callable(cn) or tpl:
                    hin = True
            if hin:
                if PY2 and h.description:
                    if not isinstance(h.description, text_type):
                        h["description"] = h.description.decode("utf-8")
                hooks.append(dict(name=h.name, description=h.description))
        return hooks

    def call(
        self,
        _funcname,
        _callback=None,
        _include=None,
        _exclude=None,
        _every=None,
        _mode=None,
        _args=None,
        _kwargs=None,
        *args,
        **kwargs
    ):
        """Try to execute the func method in all enabled hooks.

        .. versionchanged:: 1.7.0
            add param `_mode` and `_every`

        .. deprecated:: 1.7.0
            - _callback: replaced by `_every`
            - *args: replaced by `_args`
            - **kwargs: replaced by `_kwargs`
        """
        if args or kwargs:
            logger.warn(
                "The args/kwargs is deprecated. Use _args/_kwargs instead."
            )
        args = _args or args
        kwargs = _kwargs or kwargs
        response = []
        for h in sorted(self.get_enabled_hooks, key=lambda h: h.name):
            if _include and isinstance(_include, (tuple, list)):
                if h.name not in _include:
                    continue
            if _exclude and isinstance(_exclude, (tuple, list)):
                if h.name in _exclude:
                    continue
            func = getattr(h.proxy, _funcname, None)
            if callable(func):
                try:
                    if args and kwargs:
                        result = func(*args, **kwargs)
                    elif kwargs:
                        result = func(**kwargs)
                    elif args:
                        result = func(*args)
                    else:
                        result = func()
                except (ValueError, TypeError, Exception) as e:
                    result = dict(code=1, msg=str(e), sender=h.name)
                else:
                    if isinstance(result, dict):
                        result["sender"] = h.name
                        if "code" not in result:
                            result["code"] = 0
                    else:
                        result = dict(code=0, sender=h.name, data=result)
                #: Use `_every` to change the hook execution result
                if callable(_every):
                    _er = _every(result)
                    if _er and isinstance(_er, dict) and "code" in _er and \
                            "sender" in _er:
                        result = _er
                response.append(result)
                if callable(_callback):
                    _callback(result)
                if _mode == "any_true":
                    if result.get("code") == 0:
                        break
        return response

    def call_intpl(self, _tplname, _include=None, _exclude=None, **context):
        result = []
        for h in sorted(self.get_enabled_hooks, key=lambda h: h.name):
            if _include and isinstance(_include, (tuple, list)):
                if h.name not in _include:
                    continue
            if _exclude and isinstance(_exclude, (tuple, list)):
                if h.name in _exclude:
                    continue
            #: tpl is a file or html code or a func
            tpl = getattr(h.proxy, "intpl_%s" % _tplname, None)
            if not tpl:
                continue
            if callable(tpl):
                tpl = tpl()
            if tpl.split(".")[-1] in ("html", "htm", "xhtml"):
                content = render_template(tpl, **context)
            else:
                if PY2 and not isinstance(tpl, text_type):
                    tpl = tpl.decode("utf-8")
                content = render_template_string(tpl, **context)
            if content:
                result.append(content)
        return Markup("".join(result))
