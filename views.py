# -*- coding: utf-8 -*-
import os
import re
import json
import main
import cache
import jinja2
import search
import urllib2
import webapp2
import operator
from pyatom import AtomFeed
from itertools import groupby
from collections import OrderedDict
from google.appengine.api import users
from models import WikiPage, WikiPageRevision, title_grouper


JINJA = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__)),
    extensions=['jinja2.ext.autoescape'])


def format_short_datetime(value):
    if value is None:
        return ''
    return value.strftime('%m-%d %H:%M')


def format_datetime(value):
    if value is None:
        return ''
    return value.strftime('%Y-%m-%d %H:%M:%S')


def format_iso_datetime(value):
    if value is None:
        return ''
    return value.strftime('%Y-%m-%dT%H:%M:%SZ')


def to_path(title):
    return '/' + WikiPage.title_to_path(title)


def urlencode(s):
    return urllib2.quote(s.encode('utf-8'))


JINJA.filters['dt'] = format_datetime
JINJA.filters['sdt'] = format_short_datetime
JINJA.filters['isodt'] = format_iso_datetime
JINJA.filters['to_path'] = to_path
JINJA.filters['urlencode'] = urlencode


class WikiPageHandler(webapp2.RequestHandler):
    def post(self, path):
        user = WikiPageHandler._get_cur_user()
        page = WikiPage.get_by_title(WikiPage.path_to_title(path))
        revision = int(self.request.POST['revision'])
        comment = self.request.POST['comment']

        if not page.can_write(user):
            self.response.status = 403
            self.response.headers['Content-Type'] = 'text/html; charset=utf-8'
            html = self._template('403.html', {'page': page})
            self._set_response_body(html, False)
            return

        try:
            page.update_content(self.request.POST['body'], revision, comment, user)
            self.response.status = 303
            self.response.location = page.absolute_url
        except ValueError as e:
            self.response.status = 406
            self.response.headers['Content-Type'] = 'text/html; charset=utf-8'
            html = self._template('406.html', {'page': page, 'errors': [e.message]})
            self._set_response_body(html, False)

    def head(self, path):
        return self.get(path, True)

    def get(self, path, head=False):
        if path == '':
            self.response.headers['Location'] = '/Home'
            self.response.status = 303
            return
        if path.find(' ') != -1:
            self.response.headers['Location'] = '/%s' % urllib2.quote(path.replace(' ', '_'))
            self.response.status = 303
            return
        if path.startswith('+') or path.startswith('-'):
            return self.get_search_result(path, head)
        if path.startswith('sp.'):
            return self.get_sp(path[3:], head)

        user = WikiPageHandler._get_cur_user()
        restype = self._get_restype()
        page = WikiPage.get_by_title(WikiPage.path_to_title(path))

        rev = self.request.GET.get('rev', 'latest')
        if rev == 'latest':
            rev = '%d' % page.revision
        rev = int(rev)

        if rev != page.revision:
            page = page.revisions.filter(WikiPageRevision.revision==rev).get()

        if not page.can_read(user):
            self.response.status = 403
            self.response.headers['Content-Type'] = 'text/html; charset=utf-8'
            html = self._template('403.html', {'page': page})
            self._set_response_body(html, False)
            return

        # custom content-type metadata?
        if restype == 'default' and page.metadata['content-type'] != 'text/x-markdown':
            self.response.headers['Content-Type'] = '%s; charset=utf-8' % str(page.metadata['content-type'])
            self._set_response_body(WikiPage.remove_metadata(page.body), head)
            return

        if restype == 'default':
            redirect = page.metadata.get('redirect', None)
            if redirect is not None:
                self.response.headers['Location'] = '/' + WikiPage.title_to_path(redirect)
                self.response.status = 303
                return

            template_data = {'page': page}
            if page.metadata.get('schema', None) == 'Blog':
                template_data['posts'] = WikiPage.get_published_posts(page.title, 20)
            elif page.revision == 0:
                self.response.status_int = 404

            self.response.headers['Content-Type'] = 'text/html; charset=utf-8'
            html = self._template('wikipage.html', template_data)
            self._set_response_body(html, head)
        elif restype == 'atom':
            pages = WikiPage.get_published_posts(page.title, 20)
            rendered = self._render_posts_atom(page.title, pages)
            self.response.headers['Content-Type'] = 'text/xml; charset=utf-8'
            self._set_response_body(rendered, head)
        elif restype == 'form':
            html = self._template('wikipage.form.html', {'page': page})
            self.response.headers['Content-Type'] = 'text/html; charset=utf-8'
            self._set_response_body(html, head)
        elif restype == 'rawbody':
            self.response.headers['Content-Type'] = 'text/plain; charset=utf-8'
            self._set_response_body(page.body, head)
        elif restype == 'body':
            self.response.headers['Content-Type'] = 'text/html; charset=utf-8'
            html = self._template('bodyonly.html', {'page': page})
            self._set_response_body(html, head)
        elif restype == 'history':
            if type(page) == WikiPageRevision:
                raise ValueError()

            self.response.headers['Content-Type'] = 'text/html; charset=utf-8'
            revisions = page.revisions.order(-WikiPageRevision.created_at)
            html = self._template('history.html', {'page': page, 'revisions': revisions})
            self._set_response_body(html, head)
        elif restype == 'json':
            self.response.headers['Content-Type'] = 'application/json'
            pagedict = {
                'title': page.title,
                'modifier': page.modifier.email() if page.modifier else None,
                'updated_at': format_iso_datetime(page.updated_at),
                'body': page.body,
                'revision': page.revision,
                'acl_read': page.acl_read,
                'acl_write': page.acl_write,
            }
            self._set_response_body(json.dumps(pagedict), head)
        else:
            self.abort(400, 'Unknown type: %s' % restype)

    def get_search_result(self, path, head):
        expression = WikiPage.path_to_title(path)
        parsed_expression = search.parse_expression(expression)
        scoretable = WikiPage.search(expression)

        positives = dict([(k, v) for k, v in scoretable.items() if v >= 0.0])
        positives = OrderedDict(sorted(positives.iteritems(),
                                       key=operator.itemgetter(1),
                                       reverse=True)[:20])
        negatives = dict([(k, abs(v)) for k, v in scoretable.items() if v < 0.0])
        negatives = OrderedDict(sorted(negatives.iteritems(),
                                       key=operator.itemgetter(1),
                                       reverse=True)[:20])

        self.response.headers['Content-Type'] = 'text/html; charset=utf-8'
        html = self._template('search.html', {'expression': expression,
                                              'parsed_expression': parsed_expression,
                                              'positives': positives,
                                              'negatives': negatives})
        self._set_response_body(html, head)

    def get_sp(self, title, head):
        user = WikiPageHandler._get_cur_user()
        if title == 'titles':
            self.get_sp_titles(user, head)
        elif title == 'changes':
            self.get_sp_changes(user, head)
        elif title == 'index':
            self.get_sp_index(user, head)
        elif title == 'posts':
            self.get_sp_posts(user, head)
        elif title == 'search':
            self.get_sp_search(user, head)
        elif title == 'opensearch':
            self.get_sp_opensearch(user, head)
        elif title == 'randomly_update_related_pages':
            recent = self.request.GET.get('recent', '0')
            titles = WikiPage.randomly_update_related_links(50, recent == '1')
            self.response.headers['Content-Type'] = 'text/plain; charset=utf-8'
            self.response.write('\n'.join(titles))
        elif title == 'migrate':
            pass
        elif title == 'fix_suggested_pages':
            self.response.headers['Content-Type'] = 'text/plain; charset=utf-8'
            index = int(self.request.GET.get('index', '0'))
            pages = WikiPage.query().fetch(100, offset=index * 100)
            for page in pages:
                keys = [key for key in page.related_links.keys() if key.find('/') != -1]
                if len(keys) == 0:
                    continue
                else:
                    self.response.write('%s\n' % page.title)
                    for key in keys:
                        del page.related_links[key]
                        self.response.write('%s\n' % key)
                    page.put()
        elif title == 'fix_comment':
            self.response.headers['Content-Type'] = 'text/plain; charset=utf-8'
            index = int(self.request.GET.get('index', '0'))
            pages = WikiPage.query().fetch(100, offset=index * 100)
            for page in pages:
                page.comment = ''
                page.put()
        elif title == 'fix_links':
            self.response.headers['Content-Type'] = 'text/plain; charset=utf-8'

            def fix_it(links):
                for key, values in links.items():
                    if len(key.split('/')) == 2:
                        continue
                    if key == u'deathDate':
                        newkey = 'Person/%s' % key
                    elif key == u'birthDate':
                        newkey = 'Person/%s' % key
                    elif key == u'datePublished':
                        newkey = 'Book/%s' % key
                    elif key == u'author':
                        newkey = 'Book/%s' % key
                    else:
                        continue

                    if newkey not in links:
                        links[newkey] = values
                    else:
                        links[newkey] = list(set(links[newkey] + values))

                    del links[key]

            page = WikiPage.query(WikiPage.title == self.request.GET['title']).fetch()[0]
            fix_it(page.inlinks)
            fix_it(page.outlinks)
            page.put()
            self.response.write('Done')
        elif title == 'gcstest':
            import cloudstorage as gcs
            f = gcs.open(
                '/ecogwiki/test.txt', 'w',
                content_type='text/plain',
                retry_params=gcs.RetryParams(backoff_factor=1.1),
                options={'x-goog-acl': 'public-read'},
            )
            f.write('Hello')
            f.close()
            self.response.write('Done')
        else:
            self.abort(404)

    def get_sp_changes(self, user, head):
        restype = self._get_restype()
        email = user.email() if user is not None else 'None'
        rendered = None

        if restype == 'default':
            if rendered is None:
                pages = WikiPage.get_changes(user)
                rendered = self._template('wiki_sp_changes.html',
                                          {'pages': pages})
            self.response.headers['Content-Type'] = 'text/html; charset=utf-8'
            self._set_response_body(rendered, head)
        elif restype == 'atom':
            if rendered is None:
                pages = WikiPage.get_changes(None, 3, include_body=True)
                config = WikiPage.yaml_by_title('.config')
                host = self.request.host_url
                url = "%s/sp.changes?_type=atom" % host
                feed = AtomFeed(title="%s: changes" % config['service']['title'],
                                feed_url=url,
                                url="%s/" % host,
                                author=config['admin']['email'])
                for page in pages:
                    feed.add(title=page.title,
                             content_type="html",
                             content=page.rendered_body,
                             author=page.modifier,
                             url='%s%s' % (host, page.absolute_url),
                             updated=page.updated_at)
                rendered = feed.to_string()
            self.response.headers['Content-Type'] = 'text/xml; charset=utf-8'
            self._set_response_body(rendered, head)
        else:
            self.abort(400, 'Unknown type: %s' % restype)

    def get_sp_posts(self, user, head):
        restype = self._get_restype()
        email = user.email() if user is not None else 'None'
        rendered = None

        if restype == 'default':
            if rendered is None:
                pages = WikiPage.get_published_posts(None, 200)
                rendered = self._template('wiki_sp_posts.html',
                                          {'pages': pages})
            self.response.headers['Content-Type'] = 'text/html; charset=utf-8'
            self._set_response_body(rendered, head)
        elif restype == 'atom':
            if rendered is None:
                pages = WikiPage.get_published_posts(None, 20)
                rendered = self._render_posts_atom(None, pages)
            self.response.headers['Content-Type'] = 'text/xml; charset=utf-8'
            self._set_response_body(rendered, head)
        else:
            self.abort(400, 'Unknown type: %s' % restype)

    def _render_posts_atom(self, title, pages):
        host = self.request.host_url
        config = WikiPage.yaml_by_title('.config')
        if title is None:
            feed_title = '%s: posts' % config['service']['title']
            url = "%s/sp.posts?_type=atom" % host
        else:
            feed_title = title
            url = "%s/%s?_type=atom" % (WikiPage.title_to_path(title), host)

        feed = AtomFeed(title=feed_title,
                        feed_url=url,
                        url="%s/" % host,
                        author=config['admin']['email'])
        for page in pages:
            feed.add(title=page.title,
                     content_type="html",
                     content=page.rendered_body,
                     author=page.modifier,
                     url='%s%s' % (host, page.absolute_url),
                     updated=page.published_at)
        return feed.to_string()

    def get_sp_index(self, user, head):
        restype = self._get_restype()
        if restype == 'default':
            pages = WikiPage.get_index(user)
            page_group = groupby(pages,
                                 lambda p: title_grouper(p.title))
            html = self._template('wiki_sp_index.html',
                                  {'page_group': page_group})
            self.response.headers['Content-Type'] = 'text/html; charset=utf-8'
            self._set_response_body(html, head)
        elif restype == 'atom':
            pages = WikiPage.get_index(None)
            config = WikiPage.yaml_by_title('.config')
            host = self.request.host_url
            url = "%s/sp.index?_type=atom" % host
            feed = AtomFeed(title="%s: title index" % config['service']['title'],
                            feed_url=url,
                            url="%s/" % host,
                            author=config['admin']['email'])
            for page in pages:
                feed.add(title=page.title,
                         content_type="html",
                         author=page.modifier,
                         url='%s%s' % (host, page.absolute_url),
                         updated=page.updated_at)
            self.response.headers['Content-Type'] = 'text/xml; charset=utf-8'
            self._set_response_body(feed.to_string(), head)
        else:
            self.abort(400, 'Unknown type: %s' % restype)

    def get_sp_search(self, user, head):
        restype = self._get_restype()
        resformat = self.request.GET.get('format', 'opensearch')
        q = self.request.GET.get('q', None)

        if restype == 'json' and resformat == 'opensearch':
            titles = WikiPage.get_titles(user)
            if q is not None and len(q) > 0:
                titles = [t for t in titles if t.find(q) != -1]
            self.response.headers['Content-Type'] = 'application/json'
            self._set_response_body(json.dumps([q, titles]), head)
        else:
            self.abort(400, 'Unknown type: %s' % restype)

    def get_sp_opensearch(self, user, head):
        self.response.headers['Content-Type'] = 'text/xml'
        rendered = self._template('opensearch.xml', {})
        self._set_response_body(rendered, head)

    def get_sp_titles(self, user, head):
        restype = self._get_restype()

        if restype == 'json':
            titles = WikiPage.get_titles(user)
            self.response.headers['Content-Type'] = 'application/json'
            self._set_response_body(json.dumps(titles), head)
        else:
            self.abort(400, 'Unknown type: %s' % restype)

    def _template(self, path, data):
        t = JINJA.get_template('templates/%s' % path)
        config = WikiPage.yaml_by_title('.config')

        data['is_local'] = self.request.host_url.startswith('http://localhost')
        data['is_mobile'] = self._is_mobile()
        data['user'] = WikiPageHandler._get_cur_user()
        data['users'] = users
        data['cur_url'] = self.request.url
        data['config'] = config
        data['app'] = {
            'version': main.VERSION,
        }
        return t.render(data)

    def _get_restype(self):
        restype = self.request.GET.get('_type', 'default')
        return restype

    def _set_response_body(self, resbody, head):
        if head:
            self.response.headers['Content-Length'] = str(len(resbody))
        else:
            self.response.write(resbody)

    def _is_mobile(self):
        p = r'.*(Android|Fennec|GoBrowser|iPad|iPhone|iPod|Mobile|Opera Mini|Opera Mobi|Windows CE).*'
        if 'User-Agent' not in self.request.headers:
            return False
        return re.match(p, self.request.headers['User-Agent']) is not None

    @staticmethod
    def _get_cur_user():
        user = users.get_current_user()
        if user is not None:
            cache.add_recent_email(user.email())
        return user
