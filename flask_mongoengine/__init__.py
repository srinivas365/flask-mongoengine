import inspect

import mongoengine
from flask import Flask, abort, current_app
from mongoengine.base.fields import BaseField
from mongoengine.errors import DoesNotExist
from mongoengine.queryset import QuerySet

from .connection import *
from .json import override_json_encoder
from .pagination import *
from .sessions import *
from .wtf import WtfBaseField

VERSION = (1, 0, 0)


def get_version():
    """Return the VERSION as a string."""
    return ".".join(map(str, VERSION))


__version__ = get_version()


def _patch_base_field(obj, name):
    """
    If the object submitted has a class whose base class is
    mongoengine.base.fields.BaseField, then monkey patch to
    replace it with flask_mongoengine.wtf.WtfBaseField.

    @note:  WtfBaseField is an instance of BaseField - but
            gives us the flexibility to extend field parameters
            and settings required of WTForm via model form generator.

    @see: flask_mongoengine.wtf.base.WtfBaseField.
    @see: model_form in flask_mongoengine.wtf.orm

    @param obj: MongoEngine instance in which we should locate the class.
    @param name: Name of an attribute which may or may not be a BaseField.
    """
    # TODO is there a less hacky way to accomplish the same level of
    # extensibility/control?

    # get an attribute of the MongoEngine class and return if it's not
    # a class
    cls = getattr(obj, name)
    if not inspect.isclass(cls):
        return

    # if it is a class, inspect all of its parent classes
    cls_bases = list(cls.__bases__)

    # if any of them is a BaseField, replace it with WtfBaseField
    for index, base in enumerate(cls_bases):
        if base == BaseField:
            cls_bases[index] = WtfBaseField
            cls.__bases__ = tuple(cls_bases)
            break

    # re-assign the class back to the MongoEngine instance
    delattr(obj, name)
    setattr(obj, name, cls)


def _include_mongoengine(obj):
    """
    Copy all of the attributes from mongoengine and mongoengine.fields
    onto obj (which should be an instance of the MongoEngine class).
    """
    # TODO why do we need this? What's wrong with importing from the
    # original modules?
    for attr_name in mongoengine.__all__:
        if not hasattr(obj, attr_name):
            setattr(obj, attr_name, getattr(mongoengine, attr_name))

            # patch BaseField if available
            _patch_base_field(obj, attr_name)


def current_mongoengine_instance():
    """Return a MongoEngine instance associated with current Flask app."""
    me = current_app.extensions.get("mongoengine", {})
    for k, v in me.items():
        if isinstance(k, MongoEngine):
            return k


class MongoEngine(object):
    """Main class used for initialization of Flask-MongoEngine."""

    def __init__(self, app=None, config=None):
        _include_mongoengine(self)

        self.app = None
        self.config = config
        self.Document = Document
        self.DynamicDocument = DynamicDocument

        if app is not None:
            self.init_app(app, config)

    def init_app(self, app, config=None):
        if not app or not isinstance(app, Flask):
            raise TypeError("Invalid Flask application instance")

        self.app = app

        app.extensions = getattr(app, "extensions", {})

        # Make documents JSON serializable
        override_json_encoder(app)

        if "mongoengine" not in app.extensions:
            app.extensions["mongoengine"] = {}

        if self in app.extensions["mongoengine"]:
            # Raise an exception if extension already initialized as
            # potentially new configuration would not be loaded.
            raise ValueError("Extension already initialized")

        if config:
            # Passed config have max priority, over init config.
            self.config = config

        if not self.config:
            # If no configs passed, use app.config.
            self.config = app.config

        # Obtain db connection(s)
        connections = create_connections(self.config)

        # Store objects in application instance so that multiple apps do not
        # end up accessing the same objects.
        s = {"app": app, "conn": connections}
        app.extensions["mongoengine"][self] = s

    @property
    def connection(self):
        """
        Return MongoDB connection(s) associated with this MongoEngine
        instance.
        """
        return current_app.extensions["mongoengine"][self]["conn"]


class BaseQuerySet(QuerySet):
    """Extends :class:`~mongoengine.queryset.QuerySet` class with handly methods."""

    def _abort_404(self, _message_404):
        """Returns 404 error with message, if message provided.

        :param _message_404: Message for 404 comment
        """
        abort(404, _message_404) if _message_404 else abort(404)

    def get_or_404(self, *args, _message_404=None, **kwargs):
        """Get a document and raise a 404 Not Found error if it doesn't exist.

        :param _message_404: Message for 404 comment, not forwarded to
            :func:`~mongoengine.queryset.QuerySet.get`
        :param args: args list, silently forwarded to
            :func:`~mongoengine.queryset.QuerySet.get`
        :param kwargs: keywords arguments, silently forwarded to
            :func:`~mongoengine.queryset.QuerySet.get`
        """
        try:
            return self.get(*args, **kwargs)
        except DoesNotExist:
            self._abort_404(_message_404)

    def first_or_404(self, _message_404=None):
        """
        Same as :func:`~BaseQuerySet.get_or_404`, but uses
        :func:`~mongoengine.queryset.QuerySet.first`, not
        :func:`~mongoengine.queryset.QuerySet.get`.

        :param _message_404: Message for 404 comment, not forwarded to
            :func:`~mongoengine.queryset.QuerySet.get`
        """
        return self.first() or self._abort_404(_message_404)

    def paginate(self, page, per_page, **kwargs):
        """
        Paginate the QuerySet with a certain number of docs per page
        and return docs for a given page.
        """
        return Pagination(self, page, per_page)

    def paginate_field(self, field_name, doc_id, page, per_page, total=None):
        """
        Paginate items within a list field from one document in the
        QuerySet.
        """
        # TODO this doesn't sound useful at all - remove in next release?
        item = self.get(id=doc_id)
        count = getattr(item, field_name + "_count", "")
        total = total or count or len(getattr(item, field_name))
        return ListFieldPagination(
            self, doc_id, field_name, page, per_page, total=total
        )


class Document(mongoengine.Document):
    """Abstract document with extra helpers in the queryset class"""

    meta = {"abstract": True, "queryset_class": BaseQuerySet}

    def paginate_field(self, field_name, page, per_page, total=None):
        """Paginate items within a list field."""
        # TODO this doesn't sound useful at all - remove in next release?
        count = getattr(self, field_name + "_count", "")
        total = total or count or len(getattr(self, field_name))
        return ListFieldPagination(
            self.__class__.objects, self.pk, field_name, page, per_page, total=total
        )


class DynamicDocument(mongoengine.DynamicDocument):
    """Abstract Dynamic document with extra helpers in the queryset class"""

    meta = {"abstract": True, "queryset_class": BaseQuerySet}
