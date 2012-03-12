from django.db import models
from django.db.models import Q
from django.contrib.auth.models import User
from django.conf import settings
from lang.models import Language
from django.db.models import Sum
from django.utils.translation import ugettext_lazy, ugettext as _
from django.utils.safestring import mark_safe
from django.core.mail import mail_admins
from django.core.exceptions import ValidationError
from glob import glob
import os
import os.path
import logging
import git
import traceback
from translate.storage import factory
from translate.storage import poheader
from datetime import datetime

import trans
from trans.managers import TranslationManager, UnitManager
from util import is_plural, split_plural, join_plural

logger = logging.getLogger('weblate')

def validate_repoweb(val):
    try:
        test = val % {'file': 'file.po', 'line': '9'}
    except Exception, e:
        raise ValidationError(_('Bad format string (%s)') % str(e))

class Project(models.Model):
    name = models.CharField(max_length = 100)
    slug = models.SlugField(db_index = True)
    web = models.URLField()
    mail = models.EmailField(blank = True)
    instructions = models.URLField(blank = True)

    class Meta:
        ordering = ['name']

    @models.permalink
    def get_absolute_url(self):
        return ('trans.views.show_project', (), {
            'project': self.slug
        })

    def get_path(self):
        return os.path.join(settings.GIT_ROOT, self.slug)

    def __unicode__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Create filesystem directory for storing data
        p = self.get_path()
        if not os.path.exists(p):
            os.makedirs(p)

        super(Project, self).save(*args, **kwargs)

    def get_translated_percent(self):
        translations = Translation.objects.filter(subproject__project = self).aggregate(Sum('translated'), Sum('total'))
        if translations['total__sum'] == 0:
            return 0
        return round(translations['translated__sum'] * 100.0 / translations['total__sum'], 1)


class SubProject(models.Model):
    name = models.CharField(max_length = 100, help_text = _('Name to display'))
    slug = models.SlugField(db_index = True, help_text = _('Name used in URLs'))
    project = models.ForeignKey(Project)
    repo = models.CharField(max_length = 200, help_text = _('URL of Git repository'))
    repoweb = models.URLField(
        help_text = _('Link to repository browser, use %(file)s and %(line)s as filename and line placeholders'),
        validators = [validate_repoweb])
    branch = models.CharField(max_length = 50, help_text = _('Git branch to translate'))
    filemask = models.CharField(max_length = 200, help_text = _('Mask of files to translate, use * instead of language code'))

    class Meta:
        ordering = ['name']

    @models.permalink
    def get_absolute_url(self):
        return ('trans.views.show_subproject', (), {
            'project': self.project.slug,
            'subproject': self.slug
        })

    def __unicode__(self):
        return '%s/%s' % (self.project.__unicode__(), self.name)

    def get_path(self):
        return os.path.join(self.project.get_path(), self.slug)

    def get_repo(self):
        '''
        Gets Git repository object.
        '''
        p = self.get_path()
        try:
            return git.Repo(p)
        except:
            return git.Repo.init(p)

    def get_repoweb_link(self, filename, line):
        return self.repoweb % {'file': filename, 'line': line}

    def configure_repo(self):
        '''
        Ensures repository is correctly configured and points to current remote.
        '''
        # Create/Open repo
        repo = self.get_repo()
        # Get/Create origin remote
        try:
            origin = repo.remotes.origin
        except:
            repo.git.remote('add', 'origin', self.repo)
            origin = repo.remotes.origin
        # Check remote source
        if origin.url != self.repo:
            repo.git.remote('set-url', 'origin', self.repo)
        # Update
        logger.info('updating repo %s', self.__unicode__())
        try:
            repo.git.remote('update', 'origin')
        except Exception, e:
            logger.error('Failed to update Git repo: %s', str(e))


    def configure_branch(self):
        '''
        Ensures local tracking branch exists and is checkouted.
        '''
        repo = self.get_repo()
        try:
            head = repo.heads[self.branch]
        except:
            repo.git.branch('--track', self.branch, 'origin/%s' % self.branch)
            head = repo.heads[self.branch]
        repo.git.checkout(self.branch)

    def update_branch(self):
        '''
        Updates current branch to match remote (if possible).
        '''
        repo = self.get_repo()
        logger.info('pulling from remote repo %s', self.__unicode__())
        repo.remotes.origin.update()
        try:
            repo.git.merge('origin/%s' % self.branch)
            logger.info('merged remote into repo %s', self.__unicode__())
        except Exception, e:
            status = repo.git.status()
            repo.git.merge('--abort')
            logger.warning('failed merge on repo %s', self.__unicode__())
            msg = 'Error:\n%s' % str(e)
            msg += '\n\nStatus:\n' + status
            mail_admins(
                'failed merge on repo %s' % self.__unicode__(),
                msg
            )

    def get_translation_blobs(self):
        '''
        Scans directory for translation blobs and returns them as list.
        '''
        repo = self.get_repo()
        tree = repo.tree()

        # Glob files
        prefix = os.path.join(self.get_path(), '')
        for f in glob(os.path.join(self.get_path(), self.filemask)):
            filename = f.replace(prefix, '')
            yield (
                self.get_lang_code(filename),
                filename,
                tree[filename]
                )

    def create_translations(self, force = False):
        '''
        Loads translations from git.
        '''
        for code, path, blob in self.get_translation_blobs():
            logger.info('checking %s', path)
            Translation.objects.update_from_blob(self, code, path, blob, force)

    def get_lang_code(self, path):
        '''
        Parses language code from path.
        '''
        parts = self.filemask.split('*')
        return path[len(parts[0]):-len(parts[1])]

    def save(self, *args, **kwargs):
        self.configure_repo()
        self.configure_branch()
        self.update_branch()

        super(SubProject, self).save(*args, **kwargs)

        self.create_translations()

    def get_translated_percent(self):
        translations = self.translation_set.aggregate(Sum('translated'), Sum('total'))
        if translations['total__sum'] == 0:
            return 0
        return round(translations['translated__sum'] * 100.0 / translations['total__sum'], 1)

class Translation(models.Model):
    subproject = models.ForeignKey(SubProject)
    language = models.ForeignKey(Language)
    revision = models.CharField(max_length = 40, default = '', blank = True)
    filename = models.CharField(max_length = 200)\

    translated = models.IntegerField(default = 0, db_index = True)
    fuzzy = models.IntegerField(default = 0, db_index = True)
    total = models.IntegerField(default = 0, db_index = True)

    objects = TranslationManager()

    class Meta:
        ordering = ['language__name']

    def get_fuzzy_percent(self):
        if self.total == 0:
            return 0
        return round(self.fuzzy * 100.0 / self.total, 1)

    def get_translated_percent(self):
        if self.total == 0:
            return 0
        return round(self.translated * 100.0 / self.total, 1)

    @models.permalink
    def get_absolute_url(self):
        return ('trans.views.show_translation', (), {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
            'lang': self.language.code
        })

    @models.permalink
    def get_download_url(self):
        return ('trans.views.download_translation', (), {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
            'lang': self.language.code
        })

    @models.permalink
    def get_translate_url(self):
        return ('trans.views.translate', (), {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
            'lang': self.language.code
        })

    def __unicode__(self):
        return '%s - %s' % (self.subproject.__unicode__(), _(self.language.name))

    def get_filename(self):
        return os.path.join(self.subproject.get_path(), self.filename)

    def get_store(self):
        return factory.getobject(self.get_filename())

    def check_sync(self):
        '''
        Checks whether database is in sync with git and possibly does update.
        '''
        blob = self.get_git_blob()
        self.update_from_blob(blob)

    def update_from_blob(self, blob, force = False):
        '''
        Updates translation data from blob.
        '''
        # Check if we're not already up to date
        if self.revision == blob.hexsha and not force:
            return

        logger.info('processing %s, revision has changed', self.filename)

        oldunits = set(self.unit_set.all().values_list('id', flat = True))

        # Load po file
        store = self.get_store()
        for pos, unit in enumerate(store.units):
            if not unit.istranslatable():
                continue
            newunit = Unit.objects.update_from_unit(self, unit, pos)
            try:
                oldunits.remove(newunit.id)
            except:
                pass

        # Delete not used units
        Unit.objects.filter(translation = self, id__in = oldunits).delete()

        # Update revision and stats
        self.update_stats(blob)

    def get_git_blob(self):
        '''
        Returns current Git blob for file.
        '''
        repo = self.subproject.get_repo()
        tree = repo.tree()
        return tree[self.filename]

    def update_stats(self, blob = None):
        if blob is None:
            blob = self.get_git_blob()
        self.total = self.unit_set.count()
        self.fuzzy = self.unit_set.filter(fuzzy = True).count()
        self.translated = self.unit_set.filter(translated = True).count()
        self.revision = blob.hexsha
        self.save()

    def get_author_name(self, request):
        full_name = request.user.get_full_name()
        if full_name == '':
            full_name = request.user.username
        return '%s <%s>' % (full_name, request.user.email)

    def git_commit(self, author):
        '''
        Commits translation to git.
        '''
        repo = self.subproject.get_repo()
        status = repo.git.status('--porcelain', '--', self.filename)
        if status == '':
            # No changes to commit
            return False
        logger.info('Commiting %s as %s', self.filename, author)
        repo.git.commit(
            self.filename,
            author = author,
            m = settings.COMMIT_MESSAGE
            )
        return True

    def update_unit(self, unit, request):
        '''
        Updates backend file and unit.
        '''
        store = self.get_store()
        src = unit.get_source_plurals()[0]
        need_save = False
        for pounit in store.findunits(src):
            if pounit.getcontext() == unit.context:
                if hasattr(pounit.target, 'strings'):
                    potarget = join_plural(pounit.target.strings)
                else:
                    potarget = pounit.target
                if unit.target != potarget or unit.fuzzy != pounit.isfuzzy():
                    pounit.markfuzzy(unit.fuzzy)
                    if unit.is_plural():
                        pounit.settarget(unit.get_target_plurals())
                    else:
                        pounit.settarget(unit.target)
                    need_save = True
                # We should have only one match
                break
        if need_save:
            author = self.get_author_name(request)
            if hasattr(store, 'updateheader'):
                po_revision_date = datetime.now().strftime('%Y-%m-%d %H:%M') + poheader.tzstring()

                store.updateheader(
                    add = True,
                    last_translator = author,
                    plural_forms = self.language.get_plural_form(),
                    language = self.language.code,
                    PO_Revision_Date = po_revision_date,
                    x_generator = 'Weblate %s' % trans.VERSION
                    )
            store.save()
            self.git_commit(author)

        return need_save, pounit

    def get_checks(self):
        '''
        Returns list of failing checks on current translation.
        '''
        result = [('all', _('All strings'))]
        nottranslated = self.unit_set.filter_type('untranslated').count()
        fuzzy = self.unit_set.filter_type('fuzzy').count()
        suggestions = self.unit_set.filter_type('suggestions').count()
        if nottranslated > 0:
            result.append(('untranslated', _('Not translated strings (%d)') % nottranslated))
        if fuzzy > 0:
            result.append(('fuzzy', _('Fuzzy strings (%d)') % fuzzy))
        if suggestions > 0:
            result.append(('suggestions', _('Strings with suggestions (%d)') % suggestions))
        return result

    def merge_store(self, author, store2, overwrite, mergefuzzy = False):
        store1 = self.get_store()
        store1.require_index()

        for unit2 in store2.units:
            if unit2.isheader():
                if isinstance(store1, poheader.poheader):
                    store1.mergeheaders(store2)
                continue
            unit1 = store1.findid(unit2.getid())
            if unit1 is None:
                unit1 = store1.findunit(unit2.source)
            if unit1 is None:
                logger.error("The template does not contain the following unit:\n%s", str(unit2))
            else:
                if len(unit2.target.strip()) == 0:
                    continue
                if not mergefuzzy:
                    if unit2.isfuzzy():
                        continue
                unit1.merge(unit2, overwrite=overwrite)
        store1.save()
        ret = self.git_commit(author)
        self.check_sync()
        return ret

    def merge_upload(self, request, fileobj, overwrite, mergefuzzy = False):
        # Needed to behave like something what translate toolkit expects
        fileobj.mode = "r"
        store2 = factory.getobject(fileobj)
        author = self.get_author_name(request)

        ret = False

        for s in Translation.objects.filter(language = self.language, subproject__project = self.subproject.project):
            ret |= s.merge_store(author, store2, overwrite, mergefuzzy)

        return ret

class Unit(models.Model):
    translation = models.ForeignKey(Translation)
    checksum = models.CharField(max_length = 40, default = '', blank = True, db_index = True)
    location = models.TextField(default = '', blank = True)
    context = models.TextField(default = '', blank = True)
    comment = models.TextField(default = '', blank = True)
    flags = models.TextField(default = '', blank = True)
    source = models.TextField()
    target = models.TextField(default = '', blank = True)
    fuzzy = models.BooleanField(default = False, db_index = True)
    translated = models.BooleanField(default = False, db_index = True)
    position = models.IntegerField(db_index = True)

    objects = UnitManager()

    class Meta:
        ordering = ['position']

    def update_from_unit(self, unit, pos, force):
        location = ', '.join(unit.getlocations())
        if hasattr(unit, 'typecomments'):
            flags = ', '.join(unit.typecomments)
        else:
            flags = ''
        if hasattr(unit.target, 'strings'):
            target = join_plural(unit.target.strings)
        else:
            target = unit.target
        fuzzy = unit.isfuzzy()
        translated = unit.istranslated()
        comment = unit.getnotes()
        if not force and location == self.location and flags == self.flags and target == self.target and fuzzy == self.fuzzy and translated == self.translated and comment == self.comment and pos == self.position:
            return
        self.position = pos
        self.location = location
        self.flags = flags
        self.target = target
        self.fuzzy = fuzzy
        self.translated = translated
        self.comment = comment
        self.save(force_insert = force, backend = True)

    def is_plural(self):
        return is_plural(self.source)

    def get_source_plurals(self):
        return split_plural(self.source)

    def get_target_plurals(self):
        if not self.is_plural():
            return self.target
        ret = split_plural(self.target)
        plurals = self.translation.language.nplurals
        if len(ret) == plurals:
            return ret

        while len(ret) < plurals:
            ret.append('')

        while len(ret) > plurals:
            del(ret[-1])

        return ret

    def save_backend(self, request, propagate = True):
        # Store to backend
        (saved, pounit) = self.translation.update_unit(self, request)
        self.translated = pounit.istranslated()
        if hasattr(pounit, 'typecomments'):
            self.flags = ', '.join(pounit.typecomments)
        else:
            self.flags = ''
        self.save(backend = True)
        self.translation.update_stats()
        # Propagate to other projects
        if propagate:
            allunits = Unit.objects.filter(
                checksum = self.checksum,
                translation__subproject__project = self.translation.subproject.project,
                translation__language = self.translation.language
            ).exclude(id = self.id)
            for unit in allunits:
                unit.target = self.target
                unit.fuzzy = self.fuzzy
                unit.save_backend(request, False)

    def save(self, *args, **kwargs):
        if not 'backend' in kwargs:
            logger.error('Unit.save called without backend sync: %s', ''.join(traceback.format_stack()))
        else:
            del kwargs['backend']
        super(Unit, self).save(*args, **kwargs)

    def get_location_links(self):
        ret = []
        if len(self.location) == 0:
            return ''
        for location in self.location.split(','):
            location = location.strip()
            filename, line = location.split(':')
            link = self.translation.subproject.get_repoweb_link(filename, line)
            ret.append('<a href="%s">%s</a>' % (link, location))
        return mark_safe('\n'.join(ret))

    def suggestions(self):
        return Suggestion.objects.filter(
            checksum = self.checksum,
            project = self.translation.subproject.project,
            language = self.translation.language
        )

class Suggestion(models.Model):
    checksum = models.CharField(max_length = 40, default = '', blank = True, db_index = True)
    target = models.TextField()
    user = models.ForeignKey(User, null = True, blank = True)
    project = models.ForeignKey(Project)
    language = models.ForeignKey(Language)

    def accept(self, request):
        allunits = Unit.objects.filter(
            checksum = self.checksum,
            translation__subproject__project = self.project,
            translation__language = self.language
        )
        for unit in allunits:
            unit.target = self.target
            unit.fuzzy = False
            unit.save_backend(request, False)

CHECK_CHOICES = (
    ('same', ugettext_lazy('Not translated')),
)

class Check(models.Model):
    checksum = models.CharField(max_length = 40, default = '', blank = True, db_index = True)
    project = models.ForeignKey(Project)
    language = models.ForeignKey(Language)
    check = models.CharField(max_length = 20, choices = CHECK_CHOICES)
    ignore = models.BooleanField(db_index = True)
