import copy
import datetime
import httplib
import inspect
import itertools
import logging
import time

from StringIO import StringIO

from django.conf import settings
from django.views.generic import View
from django.utils.decorators import classonlymethod
from django import http
from django.template import RequestContext, TemplateDoesNotExist
from django.shortcuts import render_to_response
from django.utils.cache import patch_vary_headers

from django_conneg.http import MediaType
from django_conneg.decorators import renderer

logger = logging.getLogger(__name__)

class ContentNegotiatedView(View):
    _renderers = None
    _renderers_by_format = None
    _renderers_by_mimetype = None
    _default_format = None
    _force_fallback_format = None
    _format_override_parameter = 'format'
    _override_priority = None

    _tcn_enabled = True
    _not_acceptable = False
    _multiple_choices = False
    _server_choice = False

    _prefetched = False
    _template_used = None
    _context_used = None


    @classonlymethod
    def as_view(cls, **initkwargs):

        renderers_by_format = {}
        renderers_by_mimetype = {}
        renderers = []

        cls.set_priority_overrides()

        for name in dir(cls):
            value = getattr(cls, name)

            if inspect.ismethod(value) and getattr(value, 'is_renderer', False):
                if value.mimetypes is not None:
                    mimetypes = value.mimetypes
                elif value.format in renderers_by_format:
                    mimetypes = renderers_by_format[value.format].mimetypes
                else:
                    mimetypes = ()
                for mimetype in mimetypes:
                    if mimetype not in renderers_by_mimetype:
                        renderers_by_mimetype[mimetype] = []
                    renderers_by_mimetype[mimetype].append(value)
                if value.format not in renderers_by_format:
                    renderers_by_format[value.format] = []
                renderers_by_format[value.format].append(value)
                renderers.append(value)

        # Order all the renderers by priority
        renderers.sort(key=lambda renderer:-renderer.priority)
        renderers = tuple(renderers)

        initkwargs.update({
            '_renderers': renderers,
            '_renderers_by_format': renderers_by_format,
            '_renderers_by_mimetype': renderers_by_mimetype,
        })

        view = super(ContentNegotiatedView, cls).as_view(**initkwargs)

        view._renderers = renderers
        view._renderers_by_format = renderers_by_format
        view._renderers_by_mimetype = renderers_by_mimetype

        return view

    def get_renderers(self, request):
        renderers = []
        if request.META.get('HTTP_NEGOTIATE'):
            negotiate = self.parse_negotiate_header(request.META['HTTP_NEGOTIATE'])
        if 'format' in request.REQUEST:
            formats = request.REQUEST[self._format_override_parameter].split(',')
            renderers, seen_formats = [], set()
            for format in formats:
                if format in self._renderers_by_format and format not in seen_formats:
                    renderers.extend(self._renderers_by_format[format])
        elif request.META.get('HTTP_ACCEPT'):
            accepts = self.parse_accept_header(request.META['HTTP_ACCEPT'])
            renderers = MediaType.resolve(accepts, self._renderers)
        elif self._default_format:
            renderers = self._renderers_by_format[self._default_format]
        if self._force_fallback_format:
            renderers.extend(self._renderers_by_format[self._force_fallback_format])
        return renderers

    def render(self, request, context, template_name):
        # We save context and template
        # in the instance so we can retrieve them
        # when cycling over all alternates.
        self._template_used = template_name
        self._context_used = context

        status_code = context.pop('status_code', httplib.OK)
        additional_headers = context.pop('additional_headers', {})

        if not hasattr(request, 'renderers'):
            request.renderers = self.get_renderers(request)

        for renderer in request.renderers:
            response = renderer(self, request, context, template_name)
            if response is NotImplemented:
                continue
            response.status_code = status_code
            response.renderer = renderer
            break
        else:
            self._not_acceptable = True


        # Transparent Content Negotiation
        # TODO properly, we should only do it if we're serving
        # HTTP/1.1, and not for 1.0 --> self._tcn_enabled flag

        if self._tcn_enabled:
            if getattr(self, '_tcn', False):
                additional_headers['Alternates'] = self.get_alternates_header()
                if self._server_choice:
                    additional_headers['TCN'] = "choice"
                else:
                    additional_headers['TCN'] = "list"
                    self._multiple_choices = True

        if self._need_variant_list():
            response = self._get_variant_list()

        for key, value in additional_headers.iteritems():
            response[key] = value

        # We're doing content-negotiation, so tell the user-agent that the
        # response will vary depending on the accept header (and negotiate
        # if we're doing it)

        varying = ("Accept",)
        if self._tcn_enabled:
            varying = varying + ("Negotiate",)
        patch_vary_headers(response, varying)
        return response

    def _get_mimetypes(self, renderers):
         return list(itertools.chain(*[r.mimetypes for r in renderers]))

    def _need_variant_list(self):
        return bool(self._multiple_choices or self._not_acceptable)

    def _get_variant_fun(self):
        """
        Returns a NOT ACCEPTABLE or a MULTIPLE_CHOICES
        type of response. Former takes precedence (i.e., malformed
        negotiate header)
        """
        if self._not_acceptable:
            return self.http_not_acceptable
        if self._multiple_choices:
            return self.http_multiple_choices

    def _get_variant_list(self):
        variant_fun = self._get_variant_fun()
        tried_mimetypes = self._get_mimetypes(self.request.renderers)
        response = variant_fun(self.request, tried_mimetypes)
        response.renderer = None
        return response

    def get_alternates_header(self):
        buf = StringIO()
        fmtstr = '{"%(resource)s?format=%(_format)s" %(quality)s {type %(mimetype)s {length %(length)s}} '

        # This is kindda hackish and innefficient right now: we pre-fetch the result
        # and cycle over all formats to compose the alternates with the right
        # length. We probably should cache this here.

        if not self._prefetched:
            # On prefetch render, we store on the instance
            #the context and template used.
            #avoid infinite recursion!
            self._prefetched = True
            nhead = 'HTTP_NEGOTIATE'
            meta = self.request.META
            nval = meta[nhead]
            meta[nhead] = ""
            pre_get = self.get(self.request)
            meta[nhead] = nval

        for r in self._renderers:
            resource = self.request.path
            mime = tuple(r.mimetypes)[0].value
            fakeres = self.render_to_format(self.request,
                        self._context_used,
                        self._template_used,
                        r.format)
            res_len = fakeres.tell()
            buf.write(fmtstr % {
                                'resource': resource,
                                '_format': r.format,
                                'quality': r.quality,
                                'mimetype': mime,
                                'length': res_len})
        return buf.getvalue()

    def http_multiple_choices(self, request, tried_mimetypes, *args, **kwargs):
        multi_snippet = """<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">
<html><head>
<title>300 Multiple Choices</title>
</head><body>
<h1>Multiple Choices</h1>
Available variants:
<ul>
 %s
</ul>
</body></html>"""

        resource = self.request.path
        response = http.HttpResponse(multi_snippet % '\n '.join(
            sorted('<li><a href="%s?format=%s">%s</a> , type %s</li>' % \
                    (resource, f.format, f.name, ", ".join(m.value for m in f.mimetypes)) \
                    for f in self._renderers)),
            mimetype="text/html")
        response.status_code = httplib.MULTIPLE_CHOICES
        return response

    def http_not_acceptable(self, request, tried_mimetypes, *args, **kwargs):
        response = http.HttpResponse("""\
Your Accept header didn't contain any supported media ranges.

Supported ranges are:

 * %s\n""" % '\n * '.join(sorted('%s (%s; %s)' % (f.name, ", ".join(m.value for m in f.mimetypes), f.format) for f in self._renderers
     #why?? breaks the 406 when negotiate:foobar
     #shouldn't it return all variants, always??
     #if not any(m in tried_mimetypes for m in f.mimetypes)
     )), mimetype="text/plain")
        response.status_code = httplib.NOT_ACCEPTABLE
        return response

    def options(self, request, *args, **kwargs):
        response = http.HttpResponse()
        response['Accept'] = ','.join(m.upper() for m in sorted(self.http_method_names) if hasattr(self, m))
        return response

    def head(self, request, *args, **kwargs):
        return self.get(request, *args, **kwargs)

    @classonlymethod
    def set_priority_overrides(cls):
        if not cls._override_priority:
            val = getattr(settings, 'CONNEG_OVERRIDE_PRIORITY', None)
            cls._override_priority = val

        if not cls._override_priority:
            return

        overrides = dict(cls._override_priority)
        renderers = filter(lambda v: getattr(v, 'is_renderer', False),
            (cls.__dict__.itervalues()))

        for r in renderers:
            if r.format in overrides.keys():
                val = overrides[r.format]
                if isinstance(val, int):
                    setattr(r, 'priority', val)

    @classmethod
    def parse_accept_header(cls, accept):
        media_types = []
        for media_type in accept.split(','):
            try:
                media_types.append(MediaType(media_type))
            except ValueError:
                pass
        return media_types

    def parse_negotiate_header(self, negotiate):
        """
        See http://tools.ietf.org/html/rfc2295
        """
        if negotiate.lower() == "trans":
            self._tcn = True
        elif negotiate.lower() == "vlist":
            self._tcn = True
            self._vlist = True
        elif negotiate.lower() == "*":
            self._tcn = True
            self._server_choice = True
        else:
            #we don't understand what the heck the this negotiate
            #header mean. So long and thanks for all the fish.
            self._tcn = True
            self._not_acceptable = True

    def render_to_format(self, request, context, template_name, format):
        status_code = context.pop('status_code', httplib.OK)
        additional_headers = context.pop('additional_headers', {})

        for renderer in self._renderers_by_format.get(format, ()):
            response = renderer(self, request, context, template_name)
            if response is not NotImplemented:
                break
        else:
            response = self.http_not_acceptable(request, ())
            renderer = None

        response.status_code = status_code
        response.renderer = renderer
        for key, value in additional_headers.iteritems():
            response[key] = value
        return response

    def join_template_name(self, template_name, extension):
        """
        Appends an extension to a template_name or list of template_names.
        """
        if template_name is None:
            return None
        if isinstance(template_name, (list, tuple)):
            return tuple('.'.join([n, extension]) for n in template_name)
        if isinstance(template_name, basestring):
            return '.'.join([template_name, extension])
        raise AssertionError('template_name not of correct type: %r' % type(template_name))

class HTMLView(ContentNegotiatedView):
    _default_format = 'html'

    @renderer(format="html", mimetypes=('text/html', 'application/xhtml+xml'), priority=1, name='HTML')
    def render_html(self, request, context, template_name):
        template_name = self.join_template_name(template_name, 'html')
        if template_name is None:
            return NotImplemented
        try:
            return render_to_response(template_name,
                                      context, context_instance=RequestContext(request),
                                      mimetype='text/html')
        except TemplateDoesNotExist:
            return NotImplemented

class TextView(ContentNegotiatedView):
    @renderer(format="txt", mimetypes=('text/plain',), priority=1, name='Plain text')
    def render_text(self, request, context, template_name):
        template_name = self.join_template_name(template_name, 'txt')
        if template_name is None:
            return NotImplemented
        try:
            return render_to_response(template_name,
                                      context, context_instance=RequestContext(request),
                                      mimetype='text/plain')
        except TemplateDoesNotExist:
            return NotImplemented

try:
    import json
except ImportError:
    try:
        import simplejson as json
    except ImportError:
        pass

# Only define if json is available.
if 'json' in locals():
    class JSONView(ContentNegotiatedView):
        _json_indent = 0

        def preprocess_context_for_json(self, context):
            return context

        def simplify(self, value):
            if isinstance(value, datetime.datetime):
                return time.mktime(value.timetuple()) * 1000
            if isinstance(value, (list, tuple)):
                items = []
                for item in value:
                    item = self.simplify(item)
                    if item is not NotImplemented:
                        items.append(item)
                return items
            if isinstance(value, dict):
                items = {}
                for key, item in value.iteritems():
                    item = self.simplify(item)
                    if item is not NotImplemented:
                        items[key] = item
                return items
            elif type(value) in (str, unicode, int, float, long):
                return value
            elif value is None:
                return value
            else:
                logger.warning("Failed to simplify object of type %r", type(value))
                return NotImplemented

        @renderer(format='json', mimetypes=('application/json',), name='JSON')
        def render_json(self, request, context, template_name):
            context = self.preprocess_context_for_json(context)
            return http.HttpResponse(json.dumps(self.simplify(context), indent=self._json_indent),
                                     mimetype="application/json")

    class JSONPView(JSONView):
        # The query parameter to look for the callback name
        _default_jsonp_callback_parameter = 'callback'
        # The default callback name if none is provided
        _default_jsonp_callback = 'callback'

        @renderer(format='js', mimetypes=('text/javascript', 'application/javascript'), name='JavaScript (JSONP)')
        def render_js(self, request, context, template_name):
            context = self.preprocess_context_for_json(context)
            callback_name = request.GET.get(self._default_jsonp_callback_parameter,
                                            self._default_jsonp_callback)

            return http.HttpResponse('%s(%s);' % (callback_name, json.dumps(self.simplify(context), indent=self._json_indent)),
                                     mimetype="application/javascript")
