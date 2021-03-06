#!/usr/bin/env python

# gettags.py
#
# gettags.py copyright (c) 2010-2014 Mark Henkelis
# mutagen copyright (c) 2005 Joe Wreschnig, Michael Urman (mutagen is Licensed under GPL version 2.0)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Author: Mark Henkelis <mark.henkelis@tesco.net>

import os, sys
import re
import locale
import time
import traceback
import codecs

import hashlib
import zlib
import sqlite3
import optparse
import ConfigParser
from collections import defaultdict
import itertools
from operator import itemgetter

import mutagen
from mutagen import File
from mutagen.asf import ASFUnicodeAttribute     # seems to be an issue with multiple tag entries in wma files

from scanfuncs import adjust_tracknumber, truncate_number
import filelog

from movetags import empty_database

import errors
errors.catch_errors()

MULTI_SEPARATOR = '\n'
fileexclusions = ['.ds_store', 'desktop.ini', 'thumbs.db']
artextns = ['.jpg', '.bmp', '.png', '.gif']
tagsextns = ['.ac3']
#enc = locale.getpreferredencoding()
enc = sys.getfilesystemencoding()

# get ini settings
config = ConfigParser.ConfigParser()
config.optionxform = str
config.read('scan.ini')

# file exclusions
try:        
    file_name_exclusions_list = config.get('gettags', 'file_name_exclusions')
    file_name_exclusions_list = file_name_exclusions_list.lower()
except ConfigParser.NoSectionError:
    pass
except ConfigParser.NoOptionError:
    pass
file_name_exclusions = []
if file_name_exclusions_list and file_name_exclusions_list != '':
    exclusions = file_name_exclusions_list.split(',')
    for exclusion in exclusions:
        if exclusion != '': file_name_exclusions.append(exclusion)

try:        
    file_extn_exclusions_list = config.get('gettags', 'file_extension_exclusions')
    file_extn_exclusions_list = file_extn_exclusions_list.lower()
except ConfigParser.NoSectionError:
    pass
except ConfigParser.NoOptionError:
    pass
file_extn_exclusions = []
if file_extn_exclusions_list and file_extn_exclusions_list != '':
    exclusions = file_extn_exclusions_list.split(',')
    for exclusion in exclusions:
        if exclusion != '': file_extn_exclusions.append(exclusion)

# linux file stats
linux_file_modification_time = 'mtime'
try:        
    linux_file_modification_time = config.get('gettags', 'linux_file_modification_time')
except ConfigParser.NoSectionError:
    pass
except ConfigParser.NoOptionError:
    pass
linux_file_creation_time = ''
try:        
    linux_file_creation_time = config.get('gettags', 'linux_file_creation_time')
except ConfigParser.NoSectionError:
    pass
except ConfigParser.NoOptionError:
    pass

# duplicate processing
ignore_duplicate_tracks = 'n'
try:        
    ignore_duplicate_tracks = config.get('gettags', 'ignore_duplicate_tracks')
    ignore_duplicate_tracks = ignore_duplicate_tracks.lower()
except ConfigParser.NoSectionError:
    pass
except ConfigParser.NoOptionError:
    pass

# blank tag processing
ignore_blank_tags = 'n'
try:        
    ignore_blank_tags = config.get('gettags', 'ignore_blank_tags')
    ignore_blank_tags = ignore_blank_tags.lower()
except ConfigParser.NoSectionError:
    pass
except ConfigParser.NoOptionError:
    pass

# duplicate precedence
duplicate_tracks_precedence = 'flac,ogg,wma,mp3'
try:        
    duplicate_tracks_precedence = config.get('gettags', 'duplicate_tracks_precedence')
    duplicate_tracks_precedence = duplicate_tracks_precedence.lower()
except ConfigParser.NoSectionError:
    pass
except ConfigParser.NoOptionError:
    pass
mimeconv = {'flac': u"audio/x-flac", 
            'mp3': u"audio/mp3",
            'ogg': u"audio/vorbis",
            'wma': u"audio/x-ms-wma"}
mime_precedence=[]
mimes = duplicate_tracks_precedence.split(',')
for mime in mimes:
    if mime in mimeconv:
        mime_precedence.append(mimeconv[mime])

# work and virtual filename extensions
work_file_extension = '.sp'
try:        
    work_file_extension = config.get('gettags', 'work_file_extension')
    work_file_extension = work_file_extension.lower()
except ConfigParser.NoSectionError:
    pass
except ConfigParser.NoOptionError:
    pass

virtual_file_extension = '.sp'
try:        
    virtual_file_extension = config.get('gettags', 'virtual_file_extension')
    virtual_file_extension = virtual_file_extension.lower()
except ConfigParser.NoSectionError:
    pass
except ConfigParser.NoOptionError:
    pass

if work_file_extension == virtual_file_extension:
    work_virtual_extensions = {work_file_extension: 'workvirtual'}
else:
    work_virtual_extensions = {work_file_extension: 'work', virtual_file_extension: 'virtual'}

tags_file_extension = '.tags'
    
# symlinks
follow_symlinks = False
try:        
    ini_follow_symlinks = config.get('gettags', 'follow_symlinks')
    ini_follow_symlinks = ini_follow_symlinks.lower()
    if ini_follow_symlinks == 'y': follow_symlinks = True
except ConfigParser.NoSectionError:
    pass
except ConfigParser.NoOptionError:
    pass

'''
For the path supplied
    For tags files
        Extract the track files referenced
        Only process a track file once
            Reject other references from the same or different tags files
        For every referenced track
            For every new file encountered
                A new record is written to tagsfiles
                The file is added to the track list
            For every existing file encountered
                If there are no changes
                    The scannumber is tagsfiles is updated
                Else
                    The tagsfile record is updated
                The file is added to the track list
            For every existing tagsfile record where no file is encountered
                The tagsfile record is deleted
    For tracks, and tracks referenced by tags files
        For every new file encountered
            a new record is written to tags
            a blank record is written to tags_update (I, 0)
            a copy of the inserted record is written to tags_update (I, 1)
        For every existing file encountered
            if there are no changes
                the scannumber in tags is updated (so that any files not
                encountered can be flagged later)
            else
                the tags record is updated
                a copy of the old record is written to tags_update (U, 0)
                a copy of the new record is written to tags_update (U, 1)
        For every existing record where no file is encountered
            the tags record is deleted
            the old record is written to tags_update (D, 0)
            a blank record is written to tags_update (D, 1)
    For works/virtuals
        For every new file encountered
            a new record is written to workvirtuals
            a blank record is written to workvirtuals_update (I, 0)
            a copy of the inserted record is written to workvirtuals_update (I, 1)
        For every existing file encountered
            if there are no changes
                the scannumber in workvirtuals is updated (so that any files not
                encountered can be flagged later)
            else
                the workvirtuals record is updated
                a copy of the old record is written to workvirtuals_update (U, 0)
                a copy of the new record is written to workvirtuals_update (U, 1)
        For every existing record where no file is encountered
            the workvirtuals record is deleted
            the old record is written to workvirtuals_update (D, 0)
            a blank record is written to workvirtuals_update (D, 1)
    For playlists
        These are processes as for works/virtuals but writing to playlists
        and playlists_updates instead

When processing tags subsequently, select from tags_update on scannumber 
    the update type is on both records
    record 0 is the before image, record 1 the after image
When processing workvirtuals subsequently, select from workvirtuals_update on scannumber 
    the update type is on both records
    record 0 is the before image, record 1 the after image
When processing playlists subsequently, select from playlists_update on scannumber 
    the update type is on both records
    record 0 is the before image, record 1 the after image
'''

db = None
c = None
db2 = None
c2 = None

def process_dir(scanpath, options, database):

    global db, c, db2, c2

    db = sqlite3.connect(database, check_same_thread = False)
    c = db.cursor()

    db2 = sqlite3.connect(database, check_same_thread = False)
    c2 = db2.cursor()

    logstring = "Scanning: %s" % scanpath
    filelog.write_log(logstring)
    
    c.execute('''insert into scans values (?,?)''', (None, scanpath))
    scannumber = c.lastrowid
    logstring = "Scannumber: %d" % scannumber
    filelog.write_log(logstring)

    processing_count = 1

    # process tags first

    visitedpaths = []
    for filepath, dirs, files in os.walk(scanpath, followlinks=follow_symlinks):

        filepath = os.path.abspath(os.path.realpath(filepath))
        if follow_symlinks:
            if filepath in visitedpaths:
                errorstring = "Path already visited, check symlinks: %s" % filepath
                filelog.write_error(errorstring)
                exit(1)
            visitedpaths.append(filepath)

        if type(filepath) == 'str': filepath = filepath.decode(enc, 'replace')
        if type(dirs) == 'str': dirs = [d.decode(enc, 'replace') for d in dirs]
        if type(files) == 'str': files = [f.decode(enc, 'replace') for f in files]

        if options.exclude:
            for ex in options.exclude:
                if ex in filepath:
                    continue

        # check for any .tags files found and expand them
        tagfiles = []
        for fn in files:
            ff, ex = os.path.splitext(fn)
            if ex.lower() == tags_file_extension:
                ffn = os.path.join(filepath, fn)
                success, created, lastmodified, fsize, filler = getfilestat(ffn)

                # check whether file has changed
                save_tagsfile_tags = None
                try:
                    c.execute("""select created, lastmodified from tagsfiles where path=? and filename=?""",
                                (filepath, fn))
                    row = c.fetchone()
                    if not row:
                        # file is new
                        save_tagsfile_tags = 'I'
                    else:
                        create, lastmod = row
                        if create == created and lastmod == lastmodified:
                            # file has not been updated
                            save_tagsfile_tags = 'S'
                        else:
                            # file has been updated
                            save_tagsfile_tags = 'U'
                               
                except sqlite3.Error, e:
                    errorstring = "Error reading tagsfile record: %s" % e.args[0]
                    filelog.write_error(errorstring)

                # extract file contents
                tagfiletracks = read_workvirtualfile(ffn, ex.lower(), filepath, database)

                for tagfile in tagfiletracks:

#                    print "----tagfile---- %s " % str(tagfile)

                    wvnumber, wvfile, wvfilecreated, wvfilelastmodified, plfile, plfilecreated, plfilelastmodified, trackfile, trackfilecreated, trackfilelastmodified, wvtype, wvtitle, wvartist, wvalbumartist, wvcomposer, wvyear, wvgenre, wvcover, wvdiscnumber, wvoccurs, wvinserted, wvcreated, wvlastmodified, wvtitlesort, wvalbumsort, wvartistsort, wvalbumartistsort, wvcomposersort, tracktitle, tracklength, wvtracknumber = tagfile
                    trackfilepath, trackfilename = os.path.split(trackfile)
                    wvcoverartid = str(get_art_id(c, wvcover))
                    wvtracknumber = str(wvtracknumber)

                    # tracks can only appear in the database once, unless via a virtual.
                    # We need to check whether the current track has been created before
                    # a) in this .tags file (use wvoccurs)
                    # b) in another .tags file (check database)
                    # If we find a duplicate skip it
                    if wvoccurs != 0:
                        logstring = "Tagsfile track duplicate encountered in this tagsfile, skipping. Track: %s  Tagsfile: %s, %s" % (trackfile, fn, filepath)
                        filelog.write_verbose_log(logstring)
                        continue
                    else:
                        try:
                            # get the existing record for this track if it exists
                            c.execute("""select path, filename from tagsfiles 
                                         where trackfile=?""",
                                         (trackfile, ))
                            crow = c.fetchone()
                            if crow:
                                epath, efilename = crow
                                if epath != filepath or efilename != fn:
                                    logstring = "Tagsfile track duplicate encountered, skipping. Track: %s  Existing tagsfile: %s, %s  New tagsfile: %s, %s" % (trackfile, efilename, epath, fn, filepath)
                                    filelog.write_verbose_log(logstring)
                                    continue
                        except sqlite3.Error, e:
                            errorstring = "Error checking overridden file duplicates: %s" % e.args[0]
                            filelog.write_error(errorstring)

                    currenttime = time.time()
                    inserted = currenttime
                    lastscanned = currenttime

                    # process tagsfile tracks if tagsfile is new or updated
                    # - new tracks to this tagsfile could already exist in database
                    if save_tagsfile_tags == 'S':

                        save_tagsfile_track_tags = 'S'

                    else:                    

                        # check whether the tags for this track changed
                        try:
                            # get the existing record for this file if it exists
                            c.execute("""select * from tagsfiles 
                                         where trackfile=?""",
                                         (trackfile, ))
                            crow = c.fetchone()
                            if not crow:
                                save_tagsfile_track_tags = 'I'
                            else:
                                # track exists, get data
                                o_path, o_filename, \
                                o_tracknumber, o_tracktitle, \
                                o_tracklength, o_trackfile, \
                                o_artist, o_album, o_albumartist, o_composer, \
                                o_year, o_genre, \
                                o_coverart, o_coverartid, \
                                o_discnumber, \
                                o_titlesort, o_albumsort, o_artistsort, \
                                o_albumartistsort, o_composersort, \
                                o_inserted, o_created, o_lastmodified, \
                                o_scannumber, o_lastscanned = crow

                                # check whether data has changed
                                # don't check file dates as any change will flag a change
                                # to a track when it may not have been updated
                                if o_tracknumber == wvtracknumber and \
                                   o_tracktitle == tracktitle and \
                                   o_tracklength == tracklength and \
                                   o_artist == wvartist and \
                                   o_album == wvtitle and \
                                   o_albumartist == wvalbumartist and \
                                   o_composer == wvcomposer and \
                                   o_year == wvyear and \
                                   o_genre == wvgenre and \
                                   o_coverart == wvcover and \
                                   o_coverartid == wvcoverartid and \
                                   o_discnumber == wvdiscnumber and \
                                   o_titlesort == wvtitlesort and \
                                   o_albumsort == wvalbumsort and \
                                   o_artistsort == wvartistsort and \
                                   o_albumartistsort == wvalbumartistsort and \
                                   o_composersort == wvcomposersort:

                                    # as data has not changed, just set to flag scan
                                    save_tagsfile_track_tags = 'S'

                                else:
                                    # track data exists but has changed
                                    save_tagsfile_track_tags = 'U'

                        except sqlite3.Error, e:
                            errorstring = "Error checking overridden file tags: %s" % e.args[0]
                            filelog.write_error(errorstring)

                    if save_tagsfile_track_tags == 'S':

                        try:
                            tags = (scannumber, lastscanned, trackfile)
                            logstring = "UPDATE SCAN DETAILS TAGSFILE TRACK: " + str(tags)
                            filelog.write_verbose_log(logstring)
                            
                            # TODO: check whether we should set created/lastmod too
                            
                            c.execute("""update tagsfiles set
                                         scannumber=?, lastscanned=? 
                                         where trackfile=?""",
                                         tags)

                        except sqlite3.Error, e:
                            errorstring = "Error updating tagsfile scan details: %s" % e.args[0]
                            filelog.write_error(errorstring)

                    elif save_tagsfile_track_tags == 'U':

                        try:
                            data = (wvtracknumber, tracktitle, 
                                    tracklength, 
                                    wvartist, wvtitle, wvalbumartist, wvcomposer,
                                    wvyear, wvgenre,
                                    wvcover, wvcoverartid,
                                    wvdiscnumber,
                                    wvtitlesort, wvalbumsort, wvartistsort,
                                    wvalbumartistsort, wvcomposersort,
                                    wvfilecreated, wvfilelastmodified,
                                    scannumber, lastscanned, 
                                    trackfile)
                            logstring = "Existing tags file track updated: %s, %s, %s" % (fn, trackfilename, filepath)
                            filelog.write_log(logstring)
                            logstring = "UPDATE: " + str(data)
                            filelog.write_verbose_log(logstring)
                            c.execute("""update tagsfiles set
                                         tracknumber=?, tracktitle=?,
                                         tracklength=?, 
                                         artist=?, album=?, albumartist=?, composer=?,
                                         year=?, genre=?,
                                         cover=?, coverartid=?,
                                         discnumber=?,
                                         titlesort=?, albumsort=?, artistsort=?,
                                         albumartistsort=?, composersort=?,
                                         created=?, lastmodified=?,
                                         scannumber=?, lastscanned=? 
                                         where trackfile=?""",
                                         data)
                        except sqlite3.Error, e:
                            errorstring = "Error updating tagsfile details: %s" % e.args[0]
                            filelog.write_error(errorstring)

                    else:   # must be 'I'

                        try:
                            data = (filepath, fn,
                                    wvtracknumber, tracktitle, 
                                    tracklength, trackfile, 
                                    wvartist, wvtitle, wvalbumartist, wvcomposer,
                                    wvyear, wvgenre,
                                    wvcover, wvcoverartid,
                                    wvdiscnumber,
                                    wvtitlesort, wvalbumsort, wvartistsort,
                                    wvalbumartistsort, wvcomposersort,
                                    inserted, wvfilecreated, wvfilelastmodified,
                                    scannumber, lastscanned)
                            logstring = "New tagsfile track found: %s, %s, %s" % (fn, trackfilename, filepath)
                            filelog.write_log(logstring)
                            logstring = "INSERT: " + str(data)
                            filelog.write_verbose_log(logstring)
                            c.execute("""insert into tagsfiles values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", data)
                        except sqlite3.Error, e:
                            errorstring = "Error inserting tagsfile details: %s" % e.args[0]
                            filelog.write_error(errorstring)

                    tagfiles += [tagfile + (save_tagsfile_track_tags,)]

        db.commit()

        # now look for tagfile tracks for this path that we didn't encounter 
        # - they must have been deleted or moved so delete them
        try:
            scanpathlike = "%s%s" % (scanpath, '%')
            c2.execute("""select rowid, path, filename, trackfile, scannumber from tagsfiles where scannumber != ? and path like ?""",
                        (scannumber, scanpathlike))
            for crow in c2:
                # track exists, get file data
                o_rowid, o_path, o_filename, o_trackfile, o_s = crow
                # check if we have matched a partial path
                if scanpath != o_path:
                    if o_path[len(scanpath)] != os.sep:
                        continue
                # delete record from tagsfiles
                logstring = "Existing tagsfile file not found: %s, %s, %s" % (o_trackfile, o_filename, o_path)
                filelog.write_log(logstring)
                logstring = "DELETE: " + str(crow)
                filelog.write_verbose_log(logstring)
                c.execute("""delete from tagsfiles where rowid=?""", (o_rowid,))

        except sqlite3.Error, e:
            errorstring = "Error processing tagefile track deletions: %s" % e.args[0]
            filelog.write_error(errorstring)

        db.commit()

        # get any folderart for tracks in this directory
        folderart = get_folderart(files)
        if folderart:
            folderart = os.path.join(filepath, folderart)

        files.sort()
        # add tagfile track entries to files list
        files += tagfiles

#        print 'files: %s' % files

        for entry in files:
            
            if type(entry) is tuple:
                # have a tagfiles entry rather than a file
                # - get trackfiles field (the file we are providing tags for)
                ffn = entry[7]
                fpath, fn = os.path.split(ffn)
                ff, ex = os.path.splitext(fn)
                # get art field (can have different art from folder art for .tags files)
                folderart = entry[17]
#                print 'ffn: %s' % ffn
#                print 'fn: %s' % fn
#                save_tagsfile_tags = entry[31]
                
            else:
                fn = entry
                ff, ex = os.path.splitext(fn)
                if fn.lower() in file_name_exclusions: continue
                if ex.lower() in playlist_extensions: continue
                if ex.lower() in work_virtual_extensions: continue
                if ex.lower() in artextns: continue
                if ex.lower() in file_extn_exclusions: continue
                # new two will be processed via tuple entry above
                if ex.lower() in tagsextns: continue    
                if ex.lower() == tags_file_extension: continue
                ffn = os.path.join(filepath, fn)
                
            if not os.access(ffn, os.R_OK): continue

            try:
                if options.verbose:
                    out = "processing file: " + str(processing_count) + "\r" 
                    sys.stderr.write(out)
                    sys.stderr.flush()
                    processing_count += 1

                success, created, lastmodified, fsize, filler = getfilestat(ffn)
                
                get_tags = True
                
#                print '**** fn: %s' % fn
                
                # don't process file if it hasn't changed, unless art has been added/changed
                if type(entry) is not tuple:
                    try:
                        c.execute("""select created, lastmodified, folderart from tags where path=? and filename=?""",
                                    (filepath, fn))
                        row = c.fetchone()
                        if row:
                            create, lastmod, art = row
                            if create == created and lastmod == lastmodified and art == folderart:
                                get_tags = False
                    except sqlite3.Error, e:
                        errorstring = "Error checking file created: %s" % e.args[0]
                        filelog.write_error(errorstring)

#                print "get_tags: %s, %s" % (get_tags, fn)

                tags = {}
                if get_tags:                    

                    trackart = None

                    # if tags have been provided separately for a file via .tags, don't try to get tags from track file
                    if type(entry) is tuple:

                        # create tags from those read from file
                        wvnumber, wvfile, wvfilecreated, wvfilelastmodified, plfile, plfilecreated, plfilelastmodified, trackfile, trackfilecreated, trackfilelastmodified, wvtype, wvtitle, wvartist, wvalbumartist, wvcomposer, wvyear, wvgenre, wvcover, wvdiscnumber, wvoccurs, wvinserted, wvcreated, wvlastmodified, wvtitlesort, wvalbumsort, wvartistsort, wvalbumartistsort, wvcomposersort, tracktitle, tracklength, tracknumber, save_tagsfile_tags = entry

                        tags['type'] = 'tags'    # check whether we should pass this from tags file
                        if tracktitle: tags['title'] = [tracktitle]
                        if tracklength: tags['length'] = tracklength
                        if wvtitle: tags['album'] = [wvtitle]
                        if wvartist: tags['artist'] = [wvartist]
                        if wvalbumartist: 
                            tags['albumartist'] = [wvalbumartist]
                        else:
                            if wvartist: tags['albumartist'] = [wvartist]
                        if wvcomposer: tags['composer'] = [wvcomposer]
                        if wvyear: tags['date'] = [wvyear]
                        if wvgenre: tags['genre'] = [wvgenre]
                        if wvcover: tags['folderart'] = wvcover
                        if wvdiscnumber: tags['discnumber'] = [wvdiscnumber]
                        if wvtitlesort: tags['titlesort'] = [wvtitlesort]
                        if wvalbumsort: tags['albumsort'] = [wvalbumsort]
                        if wvartistsort: tags['artistsort'] = [wvartistsort]
                        if wvalbumartistsort: tags['albumartistsort'] = [wvalbumartistsort]
                        if wvcomposersort: tags['composersort'] = [wvcomposersort]
                        tags['tracknumber'] = [str(tracknumber)]

                        # temp for WMP - check whether we should collect this from tags file
                        tags['mime'] = 'audio/mp3'

                        # store the dates for the .tags file, rather than the music file
#                        created = wvfilecreated
#                        lastmodified = wvfilelastmodified
                                
                    else:
                
                        try:
                            kind = File(ffn, easy=True)
                        except Exception:
                            # note - Mutagen raises exceptions as various types, including Exception
                            #        but we shouldn't really use Exception as the lowest common denominator here
                            etype, value, tb = sys.exc_info()
                            error = traceback.format_exception_only(etype, value)[0].strip()
                            errorstring = "Error processing file: %s : %s" % (ffn, error)
                            filelog.write_error(errorstring)
                            continue
                            
                        if isinstance(kind, mutagen.flac.FLAC):
                            if len(kind.pictures) > 0:
                                trackart_offset, trackart_length = kind.find_picture_offset()
                                trackart = 'EMBEDDED_%s,%s' % (trackart_offset, trackart_length)
                            if kind.tags:
                                tags.update(kind.tags)
                            # assume these attributes exist (note these will overwrite kind.tags)
                            tags['type'] = 'FLAC'
                            tags['length'] = kind.info.length               # seconds
                            tags['sample_rate'] = kind.info.sample_rate     # Hz
                            tags['bits_per_sample'] = kind.info.bits_per_sample     # bps
                            tags['channels'] = kind.info.channels
                            tags['mime'] = kind.mime[0]

                        elif isinstance(kind, mutagen.mp3.EasyMP3):
                            if kind.tags:
                                picture, trackart_offset, trackart_length = kind.ID3.getpicture(kind.tags)
                                if picture:
                                    trackart = 'EMBEDDED_%s,%s' % (trackart_offset, trackart_length)
                                tags.update(kind.tags)
                                if 'performer' in tags:
                                    tags['albumartist'] = tags['performer']

                            # assume these attributes exist (note these will overwrite kind.tags)
                            tags['type'] = 'MPEG %s layer %d' % (kind.info.version, kind.info.layer)
                            tags['length'] = kind.info.length               # seconds
                            tags['sample_rate'] = kind.info.sample_rate     # Hz
                            tags['bitrate'] = kind.info.bitrate             # bps
                            tags['mime'] = kind.mime[0]

                        elif isinstance(kind, mutagen.easymp4.EasyMP4):
                            if kind.tags:
                                tags.update(kind.tags)
                            # assume these attributes exist (note these will overwrite kind.tags)
                            tags['type'] = 'MPEG-4 audio'
                            tags['length'] = kind.info.length               # seconds
                            tags['sample_rate'] = kind.info.sample_rate     # Hz
                            tags['bits_per_sample'] = kind.info.bits_per_sample     # bps
                            tags['channels'] = kind.info.channels
                            tags['bitrate'] = kind.info.bitrate             # bps
                            tags['mime'] = kind.mime[0]

                        elif isinstance(kind, mutagen.asf.ASF):
                            picture, trackart_offset, trackart_length = kind.get_picture()
                            if picture:
                                trackart = 'EMBEDDED_%s,%s' % (trackart_offset, trackart_length)
                            # WMA
                            if kind.tags:
                                if u'WM/AlbumTitle' in kind.tags: tags['album'] = [v.__str__() for v in kind.tags[u'WM/AlbumTitle']]
                                if u'WM/AlbumArtist' in kind.tags: tags['albumartist'] = [v.__str__() for v in kind.tags[u'WM/AlbumArtist']]
                                if 'Author' in kind.tags: tags['artist'] = [v for v in encodeunicode(kind.tags['Author'])]
                                if 'Title' in kind.tags: tags['title'] = [v for v in encodeunicode(kind.tags['Title'])]
                                if u'WM/Genre' in kind.tags: tags['genre'] = [v.__str__() for v in kind.tags[u'WM/Genre']]
                                if u'WM/TrackNumber' in kind.tags: tags['tracknumber'] = [v.__str__() for v in kind.tags[u'WM/TrackNumber']]
                                if u'WM/Year' in kind.tags: tags['date'] = [v.__str__() for v in kind.tags[u'WM/Year']]
                                
                                if u'WM/TitleSortOrder' in kind.tags: tags['titlesort'] = [v.__str__() for v in kind.tags[u'WM/TitleSortOrder']]
                                if u'WM/AlbumSortOrder' in kind.tags: tags['albumsort'] = [v.__str__() for v in kind.tags[u'WM/AlbumSortOrder']]
                                if u'WM/ArtistSortOrder' in kind.tags: tags['artistsort'] = [v.__str__() for v in kind.tags[u'WM/ArtistSortOrder']]
                                
                            # assume these attributes exist (note these will overwrite kind.tags)
                            tags['type'] = 'Windows Media Audio'
                            tags['length'] = kind.info.length               # seconds
                            tags['sample_rate'] = kind.info.sample_rate     # Hz
                            tags['channels'] = kind.info.channels
                            tags['bitrate'] = kind.info.bitrate             # bps
                            tags['mime'] = kind.mime[0]

                        elif isinstance(kind, mutagen.oggvorbis.OggVorbis):
                            if kind.tags.sections:
                                sections = ','.join(str(s) for s in kind.tags.sections)
                                sections += ',base64flac'
                                trackart = 'EMBEDDED_%s' % sections
                                kind.tags['metadata_block_picture'] = 'removed'     # remove from tags as not needed
                            if kind.tags:
                                tags.update(kind.tags)
                            # assume these attributes exist (note these will overwrite kind.tags)
                            tags['type'] = 'Ogg Vorbis'
                            tags['length'] = kind.info.length               # seconds
                            tags['sample_rate'] = kind.info.sample_rate     # Hz
                            tags['bitrate'] = kind.info.bitrate             # bps
                            tags['mime'] = kind.mime[0]

                        else:
                            if not ex.lower() in tagsextns and not ex.lower() == tags_file_extension:
                                logstring = "Filetype not catered for: %s" % ffn
                                filelog.write_verbose_log(logstring)
                        
                    if any(tags):
                        logstring = tags
                        filelog.write_verbose_log(logstring)

                        title = MULTI_SEPARATOR.join(tags.get('title', ''))
                        artist = MULTI_SEPARATOR.join(tags.get('artist', ''))
                        album = MULTI_SEPARATOR.join(tags.get('album', ''))

                        # ignore record with no tags if appropriate:
                        #   for an insert nothing will get inserted
                        #   for an existing record that has had tags blanked out
                        #     nothing will be changed, so the record will be
                        #     deleted in the "track not encountered" code
                        if ignore_blank_tags == 'y' and (title == '' and artist == '' and album == ''):
                            continue

                        genre = MULTI_SEPARATOR.join(tags.get('genre', ''))
                        track = MULTI_SEPARATOR.join(tags.get('tracknumber', ''))
                        year = MULTI_SEPARATOR.join(tags.get('date', ''))
                        albumartist = MULTI_SEPARATOR.join(tags.get('albumartist', ''))
                        composer = MULTI_SEPARATOR.join(tags.get('composer', ''))

                        titlesort = MULTI_SEPARATOR.join(tags.get('titlesort', ''))
                        albumsort = MULTI_SEPARATOR.join(tags.get('albumsort', ''))
                        artistsort = MULTI_SEPARATOR.join(tags.get('artistsort', ''))
                        albumartistsort = MULTI_SEPARATOR.join(tags.get('albumartistsort', ''))
                        composersort = MULTI_SEPARATOR.join(tags.get('composersort', ''))

                        codec = tags['type']
                        length = tags.get('length', 0)
                        size = fsize
                        path = filepath
                        filename = fn
                        discnumber = MULTI_SEPARATOR.join(tags.get('discnumber', ''))
                        comment = MULTI_SEPARATOR.join(tags.get('comment', ''))
                        folderartid = None
                        if folderart:
                            folderartid = str(get_art_id(c, folderart))
                        trackartid = None
                        if trackart:
                            trackspec = os.path.join(path, filename)
                            trackart = '%s_%s' % (trackart, trackspec)         
                            trackartid = str(get_art_id(c, trackspec))
                        bitrate = tags.get('bitrate', '')
                        bitspersample = tags.get('bits_per_sample', '')
                        channels = tags.get('channels', '')
                        samplerate = tags.get('sample_rate', '')
                        mime = tags.get('mime', '')
                                        
                currenttime = time.time()
                inserted = currenttime
                lastscanned = currenttime

                # if we got tags from a music file, process duplicates if appropriate
                # (don't process duplicates for .tags file entries
                if any(tags) and type(entry) is not tuple:
                    
                    try:
                        # check if there is an existing record for these tags if appropriate
                        if ignore_duplicate_tracks == 'y':
#                            c.execute("""select path, filename, mime from tags where title=? and album=? and artist=? and track=?""",
                            c.execute("""select path, filename, mime from tags where title=? collate NOCASE and album=? collate NOCASE and artist=? collate NOCASE and track=?""",
                                        (title, album, artist, str(track)))
                            crow = c.fetchone()
                            if crow:
                                duppath, dupfilename, dupmime = crow
                                # check that we haven't just found the track we're processing
                                if duppath != path or dupfilename != filename:
                                    # check if the file referred to by the existing record still exists
                                    dupspec = os.path.join(duppath, dupfilename)
                                    if os.access(dupspec, os.R_OK):
                                        # check if the track we are processing has precedence
                                        try:
                                            old_prec = mime_precedence.index(dupmime)
                                        except ValueError:
                                            # not found, set precedence to high values - 1
                                            old_prec = 998
                                        try:
                                            new_prec = mime_precedence.index(mime)
                                        except ValueError:
                                            # not found, set precedence to high values
                                            new_prec = 999
                                        if old_prec <= new_prec:
                                            # ignore the record we are processing
                                            continue
                                        # at this point we have a duplicate that needs to replace an existing track
                                        # we need to delete the old record
                                        try:
                                            c.execute("""select * from tags where path=? and filename=?""", (duppath, dupfilename))
                                            crow = c.fetchone()

                                            # get data
                                            o_id, o_id2, o_title, o_artist, o_album, \
                                            o_genre, o_track, o_year, \
                                            o_albumartist, o_composer, o_codec,  \
                                            o_length, o_size,  \
                                            o_created, o_path, o_filename,  \
                                            o_discnumber, o_comment,  \
                                            o_folderart, o_trackart,  \
                                            o_bitrate, o_samplerate,  \
                                            o_bitspersample, o_channels, o_mime,  \
                                            o_lastmodified, o_scannumber,  \
                                            o_folderartid, o_trackartid,  \
                                            o_inserted, o_lastscanned, \
                                            o_titlesort, o_albumsort, o_artistsort, \
                                            o_albumartistsort, o_composersort = crow
                                            # create audit records
                                            tags = (o_id, o_id2,
                                                    o_title, o_artist, o_album,
                                                    o_genre, o_track, o_year,
                                                    o_albumartist, o_composer, o_codec, 
                                                    o_length, o_size, 
                                                    o_created, o_path, o_filename, 
                                                    o_discnumber, o_comment, 
                                                    o_folderart, o_trackart, 
                                                    o_bitrate, o_samplerate, 
                                                    o_bitspersample, o_channels, o_mime, 
                                                    o_lastmodified, scannumber,
                                                    o_folderartid, o_trackartid,
                                                    o_inserted, o_lastscanned,
                                                    o_titlesort, o_albumsort, o_artistsort, 
                                                    o_albumartistsort, o_composersort)
                                            # check whether the duplicate we are deleting was created on this scan
                                            dupauditdelete = True
                                            c.execute("""select updatetype from tags_update where id=? and scannumber=?""", (o_id, scannumber))
                                            crow = c.fetchone()
                                            if crow:
                                                dupupdatetype, = crow
                                                if dupupdatetype == 'I':
                                                    # the duplicate we are deleting was created this scan,
                                                    # so we should not create audit records for a delete
                                                    dupauditdelete = False
                                            # delete any outstanding audit records for this scan
                                            c.execute("""delete from tags_update where id=? and scannumber=?""", (o_id, scannumber))
                                            if dupauditdelete:
                                                # pre
                                                dtags = tags + (0, 'D')
                                                c.execute("""insert into tags_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", dtags)
                                                # post
                                                dtags = cleartags(tags, lastscanned=lastscanned)
                                                dtags += (1, 'D')
                                                c.execute("""insert into tags_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", dtags)
                                            # delete record from tags
                                            logstring = "Duplicate file replaced: %s, %s" % (o_filename, o_path)
                                            filelog.write_log(logstring)
                                            logstring = "DELETE: " + str(tags)
                                            filelog.write_verbose_log(logstring)
                                            c.execute("""delete from tags where id=?""", (o_id,))

                                        except sqlite3.Error, e:
                                            errorstring = "Error processing duplicate deletion: %s" % e.args[0]
                                            filelog.write_error(errorstring)

                    except sqlite3.Error, e:
                        errorstring = "Error checking duplicate deletion: %s" % e.args[0]
                        filelog.write_error(errorstring)

                # if we didn't get tags, nothing has changed so we just want to update the 
                # scannumber to show we processed the file
                # for a separate tags file, we stored a flag to show if anything changed for this track
                # (record must exist as we found it earlier)
                
                if not get_tags or \
                   (type(entry) is tuple and save_tagsfile_tags == 'S'):
                    try:
                        tags = (scannumber, lastscanned,
                                filepath, fn)
                        logstring = "UPDATE SCAN DETAILS TRACK: " + str(tags)
                        filelog.write_verbose_log(logstring)
                        c.execute("""update tags set
                                     scannumber=?, lastscanned=? 
                                     where path=? and filename=?""", 
                                     tags)
                    except sqlite3.Error, e:
                        errorstring = "Error updating file scan details: %s" % e.args[0]
                        filelog.write_error(errorstring)
                        
                else:
                    # if we can process this filetype
                    if tags:
                        # we got tags as either:
                        #   the file timestamp changed (so the record exists)
                        #   the cover changed (so the record exists)
                        #   the tagsfile record for this track changed (so the record exists)
                        #   it's a new file (so the record doesn't exist)
                        #   it's a new tagsfile record (so the record doesn't exist)
                        
                        try:

                            # get the existing record for this unique path/filename if it exists
                            c.execute("""select * from tags where path=? and filename=?""", (path, filename))
                            crow = c.fetchone()
                            if not crow:
                                # this track did not previously exist, create a tags record
                                filespec = os.path.join(path, filename)
                                filespec = filespec.encode(enc, 'replace')
                                mf = hashlib.md5()
                                mf.update(filespec)
                                fid = mf.hexdigest()

                                tagspec = title + album + artist + track
                                tagspec = tagspec.encode(enc, 'replace')
                                mt = hashlib.md5()
                                mt.update(tagspec)
                                tid = mt.hexdigest()
                                
                                tags = (fid, tid,
                                        title, artist, album,
                                        genre, str(track), year,
                                        albumartist, composer, codec, 
                                        length, size, 
                                        created, path, filename, 
                                        discnumber, comment, 
                                        folderart, trackart,
                                        bitrate, samplerate, 
                                        bitspersample, channels, mime, 
                                        lastmodified, scannumber,
                                        str(folderartid), trackartid,
                                        inserted, lastscanned,
                                        titlesort, albumsort, artistsort, 
                                        albumartistsort, composersort)
                                logstring = "New file found: %s, %s" % (filename, path)
                                filelog.write_log(logstring)
                                logstring = "INSERT: " + str(tags)
                                filelog.write_verbose_log(logstring)
                                c.execute("""insert into tags values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", tags)
                                # create audit records
                                # pre
                                itags = cleartags(tags)
                                itags += (0, 'I')
                                c.execute("""insert into tags_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", itags)
                                # post
                                tags += (1, 'I')
                                c.execute("""insert into tags_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", tags)
                            else:
                                # track exists, get data
                                o_id, o_id2, o_title, o_artist, o_album, \
                                o_genre, o_track, o_year, \
                                o_albumartist, o_composer, o_codec,  \
                                o_length, o_size,  \
                                o_created, o_path, o_filename,  \
                                o_discnumber, o_comment,  \
                                o_folderart, o_trackart,  \
                                o_bitrate, o_samplerate, \
                                o_bitspersample, o_channels, o_mime, \
                                o_lastmodified, o_scannumber,  \
                                o_folderartid, o_trackartid,  \
                                o_inserted, o_lastscanned, \
                                o_titlesort, o_albumsort, o_artistsort, \
                                o_albumartistsort, o_composersort = crow

                                # at this point something has been updated:
                                # create audit records
                                # pre
                                tags = (o_id, o_id2,
                                        o_title, o_artist, o_album,
                                        o_genre, o_track, o_year,
                                        o_albumartist, o_composer, o_codec, 
                                        o_length, o_size, 
                                        o_created, o_path, o_filename, 
                                        o_discnumber, o_comment, 
                                        o_folderart, o_trackart, 
                                        o_bitrate, o_samplerate, 
                                        o_bitspersample, o_channels, o_mime, 
                                        o_lastmodified, scannumber,
                                        o_folderartid, o_trackartid,
                                        o_inserted, o_lastscanned,
                                        o_titlesort, o_albumsort, o_artistsort, 
                                        o_albumartistsort, o_composersort)
                                tags += (0, 'U')
                                c.execute("""insert into tags_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", tags)
                                # create new id2 in case attribs have changed
                                tagspec = title + album + artist + track
                                tagspec = tagspec.encode(enc, 'replace')
                                mt = hashlib.md5()
                                mt.update(tagspec)
                                tid = mt.hexdigest()
                                # post
                                tags = (o_id, tid,
                                        title, artist, album,
                                        genre, str(track), year,
                                        albumartist, composer, codec, 
                                        length, size, 
                                        created, path, filename,
                                        discnumber, comment,
                                        folderart, trackart, 
                                        bitrate, samplerate, 
                                        bitspersample, channels, mime, 
                                        lastmodified, scannumber, 
                                        folderartid, trackartid,
                                        o_inserted, lastscanned,
                                        titlesort, albumsort, artistsort, 
                                        albumartistsort, composersort)
                                tags += (1, 'U')
                                c.execute("""insert into tags_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", tags)
                                # now update the existing record
                                tags = (tid, title, artist, album,
                                        genre, str(track), year,
                                        albumartist, composer, codec, 
                                        length, size, 
                                        created, 
                                        discnumber, comment,
                                        folderart, trackart, 
                                        bitrate, samplerate, 
                                        bitspersample, channels, mime, 
                                        lastmodified, scannumber,
                                        folderartid, trackartid,
                                        o_inserted, lastscanned,
                                        titlesort, albumsort, artistsort, 
                                        albumartistsort, composersort,
                                        path, filename)
                                logstring = "Existing file updated: %s, %s" % (filename, path)
                                filelog.write_log(logstring)
                                logstring = "UPDATE: " + str(tags)
                                filelog.write_verbose_log(logstring)
                                c.execute("""update tags set
                                             id2=?, title=?, artist=?, album=?,
                                             genre=?, track=?, year=?,
                                             albumartist=?, composer=?, codec=?,
                                             length=?, size=?,
                                             created=?,
                                             discnumber=?, comment=?,
                                             folderart=?, trackart=?,
                                             bitrate=?, samplerate=?, 
                                             bitspersample=?, channels=?, mime=?,
                                             lastmodified=?, scannumber=?, 
                                             folderartid=?, trackartid=?, 
                                             inserted=?, lastscanned=?,
                                             titlesort=?, albumsort=?, artistsort=?, 
                                             albumartistsort=?, composersort=?
                                             where path=? and filename=?""", 
                                             tags)
                        except sqlite3.Error, e:
                            errorstring = "Error inserting/updating file tags: %s" % e.args[0]
                            filelog.write_error(errorstring)

            except KeyboardInterrupt: 
                raise

    db.commit()

    # now look for tag entries for this path that we didn't encounter - they must have been deleted or moved so flag for deletion
    try:
        scanpathlike = "%s%s" % (scanpath, '%')
        c2.execute("""select * from tags where scannumber != ? and path like ?""",
                    (scannumber, scanpathlike))
        for crow in c2:
        
            lastscanned = time.time()
            # get data
            o_id, o_id2, o_title, o_artist, o_album, \
            o_genre, o_track, o_year, \
            o_albumartist, o_composer, o_codec,  \
            o_length, o_size,  \
            o_created, o_path, o_filename,  \
            o_discnumber, o_comment,  \
            o_folderart, o_trackart,  \
            o_bitrate, o_samplerate,  \
            o_bitspersample, o_channels, o_mime,  \
            o_lastmodified, o_scannumber,  \
            o_folderartid, o_trackartid,  \
            o_inserted, o_lastscanned, \
            o_titlesort, o_albumsort, o_artistsort, \
            o_albumartistsort, o_composersort = crow
            # check if we have matched a partial path
            if scanpath != o_path:
                if o_path[len(scanpath)] != os.sep:
                    continue
            # create audit records
            tags = (o_id, o_id2,
                    o_title, o_artist, o_album,
                    o_genre, o_track, o_year,
                    o_albumartist, o_composer, o_codec, 
                    o_length, o_size, 
                    o_created, o_path, o_filename, 
                    o_discnumber, o_comment, 
                    o_folderart, o_trackart, 
                    o_bitrate, o_samplerate, 
                    o_bitspersample, o_channels, o_mime, 
                    o_lastmodified, scannumber,
                    o_folderartid, o_trackartid,
                    o_inserted, o_lastscanned,
                    o_titlesort, o_albumsort, o_artistsort, 
                    o_albumartistsort, o_composersort)
            # pre
            dtags = tags + (0, 'D')
            c.execute("""insert into tags_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", dtags)
            # post
            dtags = cleartags(tags, lastscanned=lastscanned)
            dtags += (1, 'D')
            c.execute("""insert into tags_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", dtags)
            # delete record from tags
            logstring = "Existing file not found: %s, %s" % (o_filename, o_path)
            filelog.write_log(logstring)
            logstring = "DELETE: " + str(tags)
            filelog.write_verbose_log(logstring)
            c.execute("""delete from tags where id=?""", (o_id,))

    except sqlite3.Error, e:
        errorstring = "Error processing track deletions: %s" % e.args[0]
        filelog.write_error(errorstring)

    db.commit()

    # at this point we have completed tag processing
    # it's possible that works and virtuals are on a different pathspec, so may not be processed in this run
    # we need to check each track we have changed against work and virtual entries, and flag those work and
    # virtual entries to be processed in the next pass
    # 
    # so that we can process these entries in the same loop as the workvirtual loop, we create a generator

    workvirtual_updates = get_workvirtual_update(scannumber)

    # to make sure we don't process a workvirtual twice, we use a temporary table to store what we process
    try:
        c.execute("""create temporary table tempwv (wvfile text)""")
    except sqlite3.Error, e:
        errorstring = "Error creating temporary workvirtual table: %s" % e.args[0]
        filelog.write_error(errorstring)

    db.commit()

    # we also need to check tracks against playlists (later)
    playlist_updates = get_playlist_update(scannumber)
    try:
        c.execute("""create temporary table temppl (plfile text)""")
    except sqlite3.Error, e:
        errorstring = "Error creating temporary playlist table: %s" % e.args[0]
        filelog.write_error(errorstring)

    db.commit()

    # now process works and virtuals - processing the generator first
    visitedpaths = []
    for filepath, dirs, files in itertools.chain(workvirtual_updates, os.walk(scanpath, followlinks=follow_symlinks)):

        filepath = os.path.abspath(os.path.realpath(filepath))
        if follow_symlinks:
            if filepath in visitedpaths:
                errorstring = "Path already visited, check symlinks: %s" % filepath
                filelog.write_error(errorstring)
                exit(1)
            visitedpaths.append(filepath)

        if type(filepath) == 'str': filepath = filepath.decode(enc, 'replace')
        if type(dirs) == 'str': dirs = [d.decode(enc, 'replace') for d in dirs]
        if type(files) == 'str': files = [f.decode(enc, 'replace') for f in files]

        if options.exclude:
            for ex in options.exclude:
                if ex in filepath:
                    continue
        
#        print "**** FILEPATH: %s" % filepath
        
        files.sort()

        for fn in files:
            ff, ex = os.path.splitext(fn)
            if not ex.lower() in work_virtual_extensions: continue
            ffn = os.path.join(filepath, fn)
            if not os.access(ffn, os.R_OK):
                if '..wv..' in dirs:
                    # this file was passed from the tracks scan, log an error
                    errorstring = "Track changed but unable to access workvirtual file: %s" % (ffn)
                    filelog.write_error(errorstring)
                continue

#            print "**** FFN: %s" % ffn

            try:
            
                if options.verbose:
                    out = "processing file: " + str(processing_count) + "\r" 
                    sys.stderr.write(out)
                    sys.stderr.flush()
                    processing_count += 1

                success, created, lastmodified, fsize, filler = getfilestat(ffn)

                # process works and virtuals - we will only accept tracks that are in the database

                # check whether we have processed this file before

                try:
                    c.execute("""select wvfile from temp.tempwv where wvfile=?""", (ffn, ))
                    row = c.fetchone()
                    if row:
                        continue
                    else:
                        c.execute("""insert into temp.tempwv values (?)""", (ffn, ))
                    
                except sqlite3.Error, e:
                    errorstring = "Error processing temporary workvirtual table: %s" % e.args[0]
                    filelog.write_error(errorstring)

                # read work/virtual date and track details                                    
                workvirtualtracks = read_workvirtualfile(ffn, ex.lower(), filepath, database)

                # check what has changed
                # for works and virtuals, changes include:
                #     workvirtual file has changed
                #     track referred to by workvirtual file has changed
                #     playlist in workvirtual file has changed
                #     track referred to by playlist in workvirtual file has changed
                #     tracknumber for file in workvirtual/playlist has changed
                # all these changes can result in a track change, which is what we track

                prev_wvnumber = 0
                for workvirtualtrack in workvirtualtracks:

#                    print "----workvirtualtrack----"
#                    print workvirtualtrack
#                    print

                    wvnumber, wvfile, wvfilecreated, wvfilelastmodified, plfile, plfilecreated, plfilelastmodified, trackfile, trackfilecreated, trackfilelastmodified, wvtype, wvtitle, wvartist, wvalbumartist, wvcomposer, wvyear, wvgenre, wvcover, wvdiscnumber, wvoccurs, wvinserted, wvcreated, wvlastmodified, wvtitlesort, wvalbumsort, wvartistsort, wvalbumartistsort, wvcomposersort, tracktitle, tracklength, wvtrack = workvirtualtrack

                    # check whether we have a new workvirtual (there can be more than one in a file)
                    if wvnumber != prev_wvnumber:
                        prev_wvnumber = wvnumber
                        # process workvirtual cover
                        wvcoverartid = 0
                        if wvcover:
                            # check if cover refers to embedded art (i.e. to a track in the database)
                            co_trackpath, co_trackfile = os.path.split(wvcover)
                            try:
                                c.execute("""select trackartid from tags where path=? and filename=?""", (co_trackpath, co_trackfile))
                                crow = c.fetchone()
                            except sqlite3.Error, e:
                                errorstring = "Error getting tags details for workvirtual cover: %s" % e.args[0]
                                filelog.write_error(errorstring)
                            if crow:
                                wvcoverartid, = crow
                            else:
                                # cover is an image
                                wvcoverartid = get_art_id(c, wvcover)

                    # check if any details have changed for this file/playlist/track

                    # find the track that this relates to
                    tr_trackpath, tr_trackfile = os.path.split(trackfile)
                    try:
                        c.execute("""select * from tags where path=? and filename=?""", (tr_trackpath, tr_trackfile))
                        crow = c.fetchone()
                    except sqlite3.Error, e:
                        errorstring = "Error getting tags details for workvirtual track: %s" % e.args[0]
                        filelog.write_error(errorstring)
                    if not crow:
                        # this track does not exist, reject the work/virtual record
                        errorstring = "Error processing %s: %s : %s : %s : track does not exist in database" % (wvtype, wvfile, plfile, trackfile)
                        filelog.write_error(errorstring)
                        continue
                    
                    # get track data
                    tr_id, tr_id2, tr_title, tr_artist, tr_album, \
                    tr_genre, tr_track, tr_year, \
                    tr_albumartist, tr_composer, tr_codec,  \
                    tr_length, tr_size,  \
                    tr_created, tr_path, tr_filename,  \
                    tr_discnumber, tr_comment,  \
                    tr_folderart, tr_trackart,  \
                    tr_bitrate, tr_samplerate, \
                    tr_bitspersample, tr_channels, tr_mime, \
                    tr_lastmodified, tr_scannumber,  \
                    tr_folderartid, tr_trackartid,  \
                    tr_inserted, tr_lastscanned, \
                    tr_titlesort, tr_albumsort, tr_artistsort, \
                    tr_albumartistsort, tr_composersort = crow

                    wv_change = None
                    try:
                        # when searching for a track find one that matches the occurrence we have
                        # I = not found, track needs inserting
                        # U = found, updated
                        # N = found, not updated
                        c.execute("""select wvfilecreated, wvfilelastmodified, plfilecreated, plfilelastmodified, trackfilecreated, trackfilelastmodified, track from workvirtuals where title=? and wvfile=? and plfile=? and trackfile=? and occurs=?""",
                                    (wvtitle, wvfile, plfile, trackfile, wvoccurs))
                        row = c.fetchone()
                        if row:
                            wv_change = 'U'
                            wvcreate, wvlastmod, plcreate, pllastmod, trackcreate, tracklastmod, track = row
                            if wvcreate == wvfilecreated and wvlastmod == wvfilelastmodified and \
                               plcreate == plfilecreated and pllastmod == plfilelastmodified and \
                               trackcreate == trackfilecreated and tracklastmod == trackfilelastmodified and \
                               int(track) == wvtrack:
                                wv_change = 'N'
                        else:
                            wv_change = 'I'
                    except sqlite3.Error, e:
                        errorstring = "Error checking workvirtual track created: %s" % e.args[0]
                        filelog.write_error(errorstring)

#                    print "----wvchange----"
#                    print wv_change
#                    print

                    currenttime = time.time()
                    inserted = currenttime
                    lastscanned = currenttime

                    if wv_change == 'N':
                        # nothing has changed so we just want to update the scannumber to show we processed the track
                        # (record must exist as we found it earlier)
                        try:
                            wv = (scannumber, lastscanned, wvtitle, wvfile, plfile, trackfile, wvoccurs)
                            logstring = "UPDATE SCAN DETAILS WORKVIRTUAL: " + str(wv)
                            filelog.write_verbose_log(logstring)
                            c.execute("""update workvirtuals set
                                         scannumber=?, lastscanned=? 
                                         where title=? and wvfile=? and plfile=? and trackfile=? and occurs=?""", 
                                         wv)
                        except sqlite3.Error, e:
                            errorstring = "Error updating workvirtual track scan details: %s" % e.args[0]
                            filelog.write_error(errorstring)

                    else:
                        # either we have a change or it's a new workvirtual track
                           
                        # set fields based on workvirtual content
                        wvtitle = checktag(wvtitle, tr_title)
                        wvartist = checktag(wvartist, tr_artist)
                        wvalbumartist = checktag(wvalbumartist, tr_albumartist)
                        if not wvalbumartist: wvalbumartist = wvartist
                        wvcomposer = checktag(wvcomposer, tr_composer)
                        wvyear = checktag(wvyear, tr_year)
                        wvgenre = checktag(wvgenre, tr_genre)
                        wvcover = checktag(wvcover, '')
                        wvdiscnumber = checktag(wvdiscnumber, tr_discnumber)
                        wvinserted = checktag(wvinserted, tr_inserted)
                        wvcreated = checktag(wvcreated, tr_created)
                        wvlastmodified = checktag(wvlastmodified, tr_lastmodified)
                        wvtitlesort = checktag(wvtitlesort, tr_titlesort)
                        wvalbumsort = checktag(wvalbumsort, tr_albumsort)
                        wvartistsort = checktag(wvartistsort, tr_artistsort)
                        wvalbumartistsort = checktag(wvalbumartistsort, tr_albumartistsort)
                        wvcomposersort = checktag(wvcomposersort, tr_composersort)

                        # process the track

                        if wv_change == 'I':
                        
                            # is an insert
                            # insert master and create audit records
                            try:

                                wv = (wvtitle, 
                                      wvfile, plfile, trackfile, 
                                      wvoccurs, 
                                      wvartist, wvalbumartist, wvcomposer, 
                                      wvyear, wvtrack, wvgenre, 
                                      wvcover, wvdiscnumber, 
                                      wvtype, tr_id, 
                                      wvinserted, wvcreated, wvlastmodified,
                                      wvfilecreated, wvfilelastmodified,
                                      plfilecreated, plfilelastmodified, 
                                      trackfilecreated, trackfilelastmodified, 
                                      scannumber, lastscanned,
                                      wvtitlesort, wvalbumsort, wvartistsort, 
                                      wvalbumartistsort, wvcomposersort,
                                      wvcoverartid)
                                logstring = "%s track inserted: %s : %s : %s" % (wvtype, wvfile, plfile, trackfile)
                                filelog.write_log(logstring)
                                logstring = "INSERT: " + str(wv)
                                filelog.write_verbose_log(logstring)
                                c.execute("""insert into workvirtuals values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", wv)
                                # pre                                
                                iwv = clearwv(wv)
                                iwv += (0, 'I')
                                c.execute("""insert into workvirtuals_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", iwv)
                                # post
                                iwv = wv + (1, 'I')
                                c.execute("""insert into workvirtuals_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", iwv)
                            except sqlite3.Error, e:
                                errorstring = "Error inserting workvirtual track details: %s" % e.args[0]
                                filelog.write_error(errorstring)

                        else:

                            # is an update
                            # create audit records and update master
                            try:
                                # get existing record, must be found as we got it earlier
                                c.execute("""select * from workvirtuals where title=? and wvfile=? and plfile=? and trackfile=? and occurs=?""",
                                            (wvtitle, wvfile, plfile, trackfile, wvoccurs))
                                row = c.fetchone()
                                wv_title, wv_wvfile, wv_plfile, wv_trackfile, wv_occurs, wv_artist, wv_albumartist, wv_composer, wv_year, wv_track, wv_genre, wv_cover, wv_discnumber, wv_type, wv_id, wv_inserted, wv_created, wv_lastmodified, wv_wvfilecreated, wv_wvfilelastmodified, wv_plfilecreated, wv_plfilelastmodified, wv_trackfilecreated, wv_trackfilelastmodified, wv_scannumber, wv_lastscanned, wv_titlesort, wv_albumsort, wv_artistsort, wv_albumartistsort, wv_composersort, wv_coverartid = row

                            except sqlite3.Error, e:
                                errorstring = "Error getting workvirtual details: %s" % e.args[0]
                                filelog.write_error(errorstring)

                            # check if only the workvirtual/playlist/track timestamps have changed
                            # - if so we don't need to create audit records as those changes are not
                            #   propagated into tracks

                            if wv_artist == wvartist and \
                               wv_albumartist == wvalbumartist and \
                               wv_composer == wvcomposer and \
                               wv_year == wvyear and \
                               int(wv_track) == wvtrack and \
                               wv_genre == wvgenre and \
                               wv_cover == wvcover and \
                               wv_discnumber == wvdiscnumber and \
                               wv_type == wvtype and \
                               wv_inserted == wvinserted and \
                               wv_created == wvcreated and \
                               wv_lastmodified == wvlastmodified and \
                               wv_titlesort == wvtitlesort and \
                               wv_albumsort == wvalbumsort and \
                               wv_artistsort == wvartistsort and \
                               wv_albumartistsort == wvalbumartistsort and \
                               wv_composersort == wvcomposersort and \
                               wv_coverartid == wvcoverartid:
                                try:
                                    wv = (wvfilecreated, wvfilelastmodified,
                                          plfilecreated, plfilelastmodified, 
                                          trackfilecreated, trackfilelastmodified, 
                                          scannumber, lastscanned,
                                          wvtitle, wvfile, plfile, trackfile, wvoccurs)
                                    logstring = "UPDATE: " + str(wv)
                                    filelog.write_verbose_log(logstring)
                                    c.execute("""update workvirtuals set
                                                 wvfilecreated=?, wvfilelastmodified=?,
                                                 plfilecreated=?, plfilelastmodified=?, 
                                                 trackfilecreated=?, trackfilelastmodified=?, 
                                                 scannumber=?, lastscanned=?
                                                 where title=? and wvfile=? and plfile=? and trackfile=? and occurs=?""", 
                                                 wv)
                                except sqlite3.Error, e:
                                    errorstring = "Error updating workvirtual track details: %s" % e.args[0]
                                    filelog.write_error(errorstring)

                            else:

                                # create audit records and update master
                                try:
                                    # pre                                
                                    wv = (wv_title, 
                                          wv_wvfile, wv_plfile, wv_trackfile, 
                                          wv_occurs, 
                                          wv_artist, wv_albumartist, wv_composer, 
                                          wv_year, wv_track, wv_genre, 
                                          wv_cover, wv_discnumber, 
                                          wv_type, tr_id, 
                                          wv_inserted, wv_created, wv_lastmodified,
                                          wv_wvfilecreated, wv_wvfilelastmodified,
                                          wv_plfilecreated, wv_plfilelastmodified, 
                                          wv_trackfilecreated, wv_trackfilelastmodified, 
                                          scannumber, wv_lastscanned,
                                          wv_titlesort, wv_albumsort, wv_artistsort,
                                          wv_albumartistsort, wv_composersort,
                                          wv_coverartid)
                                    wvu = wv + (0, 'U')
                                    c.execute("""insert into workvirtuals_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", wvu)
                                    # post
                                    wv = (wvtitle, 
                                          wvfile, plfile, trackfile, 
                                          wvoccurs, 
                                          wvartist, wvalbumartist, wvcomposer, 
                                          wvyear, wvtrack, wvgenre, 
                                          wvcover, wvdiscnumber, 
                                          wvtype, tr_id, 
                                          wvinserted, wvcreated, wvlastmodified,
                                          wvfilecreated, wvfilelastmodified,
                                          plfilecreated, plfilelastmodified, 
                                          trackfilecreated, trackfilelastmodified, 
                                          scannumber, lastscanned,
                                          wvtitlesort, wvalbumsort, wvartistsort,
                                          wvalbumartistsort, wvcomposersort,
                                          wvcoverartid)
                                    wvu = wv + (1, 'U')
                                    c.execute("""insert into workvirtuals_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", wvu)
                                    # now update the existing record
                                    wv = (wvartist, wvalbumartist, wvcomposer, 
                                          wvyear, wvtrack, wvgenre, 
                                          wvcover, wvdiscnumber, 
                                          wvtype, tr_id, 
                                          wvinserted, wvcreated, wvlastmodified,
                                          wvfilecreated, wvfilelastmodified,
                                          plfilecreated, plfilelastmodified, 
                                          trackfilecreated, trackfilelastmodified, 
                                          scannumber, lastscanned,
                                          wvtitlesort, wvalbumsort, wvartistsort,
                                          wvalbumartistsort, wvcomposersort,
                                          wvcoverartid,
                                          wvtitle, wvfile, plfile, trackfile, wvoccurs)
                                    logstring = "Existing workvirtual track updated: %s" % (trackfile)
                                    filelog.write_log(logstring)
                                    logstring = "UPDATE: " + str(wv)
                                    filelog.write_verbose_log(logstring)
                                    c.execute("""update workvirtuals set
                                                 artist=?, albumartist=?, composer=?, 
                                                 year=?, track=?, genre=?, 
                                                 cover=?, discnumber=?, 
                                                 type=?, id=?, 
                                                 inserted=?, created=?, lastmodified=?,
                                                 wvfilecreated=?, wvfilelastmodified=?,
                                                 plfilecreated=?, plfilelastmodified=?, 
                                                 trackfilecreated=?, trackfilelastmodified=?, 
                                                 scannumber=?, lastscanned=?,
                                                 titlesort=?, albumsort=?, artistsort=?,
                                                 albumartistsort=?, composersort=?,
                                                 coverartid=?
                                                 where title=? and wvfile=? and plfile=? and trackfile=? and occurs=?""", 
                                                 wv)
                                except sqlite3.Error, e:
                                    errorstring = "Error updating workvirtual track details: %s" % e.args[0]
                                    filelog.write_error(errorstring)

            except KeyboardInterrupt: 
                raise

    db.commit()

    # now look for workvirtual entries for this path that we didn't encounter - they must have been deleted or moved so flag for deletion
    # we use the workvirtual filespec to compare against (it's saved against each file in the set)
    try:
        scanpathlike = "%s%s" % (scanpath, '%')
        c2.execute("""select * from workvirtuals where scannumber != ? and wvfile like ?""",
                    (scannumber, scanpathlike))
        for crow in c2:
            lastscanned = time.time()
            # get data
            wv_title, wv_wvfile, wv_plfile, wv_trackfile, wv_occurs, wv_artist, wv_albumartist, wv_composer, wv_year, wv_track, wv_genre, wv_cover, wv_discnumber, wv_type, wv_id, wv_inserted, wv_created, wv_lastmodified, wv_wvfilecreated, wv_wvfilelastmodified, wv_plfilecreated, wv_plfilelastmodified, wv_trackfilecreated, wv_trackfilelastmodified, wv_scannumber, wv_lastscanned, wv_titlesort, wv_albumsort, wv_artistsort, wv_albumartistsort, wv_composersort, wv_coverartid = crow
            # check if we have matched a partial path
            if scanpath != wv_wvfile:
                if wv_wvfile[len(scanpath)] != os.sep:
                    continue
            # create audit records
            wv = (wv_title, 
                  wv_wvfile, wv_plfile, wv_trackfile, 
                  wv_occurs, 
                  wv_artist, wv_albumartist, wv_composer, 
                  wv_year, wv_track, wv_genre, 
                  wv_cover, wv_discnumber, 
                  wv_type, wv_id, 
                  wv_inserted, wv_created, wv_lastmodified,
                  wv_wvfilecreated, wv_wvfilelastmodified,
                  wv_plfilecreated, wv_plfilelastmodified, 
                  wv_trackfilecreated, wv_trackfilelastmodified, 
                  scannumber, wv_lastscanned,
                  wv_titlesort, wv_albumsort, wv_artistsort,
                  wv_albumartistsort, wv_composersort,
                  wv_coverartid)
            # pre
            wvd = wv + (0, 'D')
            c.execute("""insert into workvirtuals_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", wvd)
            # post
            wvd = clearwv(wv, lastscanned=lastscanned)
            wvd += (1, 'D')
            c.execute("""insert into workvirtuals_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", wvd)
            # delete record from tags
            logstring = "Existing workvirtual track not found: %s, %s" % (wv_wvfile, wv_trackfile)
            filelog.write_log(logstring)
            logstring = "DELETE: " + str(wv)
            filelog.write_verbose_log(logstring)
            c.execute("""delete from workvirtuals where title=? and wvfile=? and plfile=? and trackfile=? and occurs=?""", (wv_title, wv_wvfile, wv_plfile, wv_trackfile, wv_occurs))

    except sqlite3.Error, e:
        errorstring = "Error processing workvirtual track deletions: %s" % e.args[0]
        filelog.write_error(errorstring)

#    db2.commit()
    db.commit()

    # now process playlists - processing the generator first
    visitedpaths = []
    for filepath, dirs, files in itertools.chain(playlist_updates, os.walk(scanpath, followlinks=follow_symlinks)):

        filepath = os.path.abspath(os.path.realpath(filepath))
        if follow_symlinks:
            if filepath in visitedpaths:
                errorstring = "Path already visited, check symlinks: %s" % filepath
                filelog.write_error(errorstring)
                exit(1)
            visitedpaths.append(filepath)

        if type(filepath) == 'str': filepath = filepath.decode(enc, 'replace')
        if type(dirs) == 'str': dirs = [d.decode(enc, 'replace') for d in dirs]
        if type(files) == 'str': files = [f.decode(enc, 'replace') for f in files]
        
        if options.exclude:
            for ex in options.exclude:
                if ex in filepath:
                    continue
        
        files.sort()

        for fn in files:
            ff, ex = os.path.splitext(fn)
            if not ex.lower() in playlist_extensions: continue
            ffn = os.path.join(filepath, fn)
            if not os.access(ffn, os.R_OK):
                if '..pl..' in dirs:
                    # this file was passed from the tracks scan, log an error
                    errorstring = "Track changed but unable to access playlist file: %s" % (ffn)
                    filelog.write_error(errorstring)
                continue

            try:
            
                if options.verbose:
                    out = "processing file: " + str(processing_count) + "\r" 
                    sys.stderr.write(out)
                    sys.stderr.flush()
                    processing_count += 1

                success, created, lastmodified, fsize, filler = getfilestat(ffn)

                # process playlists - we will only accept tracks that are in the database

                # check whether we have processed this file before

                try:
                    c.execute("""select plfile from temp.temppl where plfile=?""", (ffn, ))
                    row = c.fetchone()
                    if row:
                        continue
                    else:
                        c.execute("""insert into temp.temppl values (?)""", (ffn, ))
                    
                except sqlite3.Error, e:
                    errorstring = "Error processing temporary playlist table: %s" % e.args[0]
                    filelog.write_error(errorstring)

                # read playlist date and track details                                    
                playlisttracks = read_playlistfile(ffn, filepath)

                # check what has changed
                # changes include:
                #     playlist has changed
                #     track referred to by playlist has changed
                #     tracknumber for file in playlist has changed
                # all these changes can result in a track change, which is what we track

                for playlisttrack in playlisttracks:

#                    print "----playlisttrack----"
#                    print playlisttrack
#                    print

                    plfile, plfilecreated, plfilelastmodified, trackfile, trackfilecreated, trackfilelastmodified, pltrack, ploccurs = playlisttrack
                    pltitle = ff
                    plid = "%X" % (zlib.crc32(plfile) & 0xffffffff)

                    # check if any details have changed for this playlist/track

                    pl_change = None
                    try:
                        # when searching for a track find one that matches the occurrence we have
                        # I = not found, track needs inserting
                        # U = found, updated
                        # N = found, not updated
                        c.execute("""select plfilecreated, plfilelastmodified, trackfilecreated, trackfilelastmodified, track from playlists where playlist=? and plfile=? and trackfile=? and occurs=?""",
                                    (pltitle, plfile, trackfile, ploccurs))
                        row = c.fetchone()
                        if row:
                            pl_change = 'U'
                            plcreate, pllastmod, trackcreate, tracklastmod, track = row
                            if plcreate == plfilecreated and pllastmod == plfilelastmodified and \
                               trackcreate == trackfilecreated and tracklastmod == trackfilelastmodified and \
                               int(track) == pltrack:
                                pl_change = 'N'
                        else:
                            pl_change = 'I'
                    except sqlite3.Error, e:
                        errorstring = "Error checking playlist track created: %s" % e.args[0]
                        filelog.write_error(errorstring)

#                    print "----plchange----"
#                    print pl_change
#                    print

                    currenttime = time.time()
                    inserted = currenttime
                    lastscanned = currenttime

                    if pl_change == 'N':
                        # nothing has changed so we just want to update the scannumber to show we processed the track
                        # (record must exist as we found it earlier)
                        try:
                            pl = (scannumber, lastscanned, pltitle, plfile, trackfile, ploccurs)
                            logstring = "UPDATE SCAN DETAILS PLAYLIST: " + str(pl)
                            filelog.write_verbose_log(logstring)
                            c.execute("""update playlists set
                                         scannumber=?, lastscanned=? 
                                         where playlist=? and plfile=? and trackfile=? and occurs=?""", 
                                         pl)
                        except sqlite3.Error, e:
                            errorstring = "Error updating playlist track scan details: %s" % e.args[0]
                            filelog.write_error(errorstring)

                    else:
                        # either we have a change or it's a new playlist track
                        # find the track that this relates to

                        tr_trackpath, tr_trackfile = os.path.split(trackfile)
                        try:
                            c.execute("""select * from tags where path=? and filename=?""", (tr_trackpath, tr_trackfile))
                            crow = c.fetchone()
                        except sqlite3.Error, e:
                            errorstring = "Error getting tags details for playlist track: %s" % e.args[0]
                            filelog.write_error(errorstring)
                        if not crow:
                            # this track does not exist
                            # check whether it's a stream uri
                            if is_stream(trackfile):
                                # for stream, create md5 of stream name instead of track spec
                                filespec = trackfile.encode(enc, 'replace')
                                mf = hashlib.md5()
                                mf.update(filespec)
                                tr_id = mf.hexdigest()
                                tr_inserted = tr_created = tr_lastmodified = None
                            else:
                                # reject the work/virtual record
                                errorstring = "Error processing playlist: %s : %s : track does not exist in database" % (plfile, trackfile)
                                filelog.write_error(errorstring)
                                continue
                            
                        else:

                            # track exists, get data
                            tr_id, tr_id2, tr_title, tr_artist, tr_album, \
                            tr_genre, tr_track, tr_year, \
                            tr_albumartist, tr_composer, tr_codec,  \
                            tr_length, tr_size,  \
                            tr_created, tr_path, tr_filename,  \
                            tr_discnumber, tr_comment,  \
                            tr_folderart, tr_trackart,  \
                            tr_bitrate, tr_samplerate, \
                            tr_bitspersample, tr_channels, tr_mime, \
                            tr_lastmodified, tr_scannumber,  \
                            tr_folderartid, tr_trackartid,  \
                            tr_inserted, tr_lastscanned, \
                            tr_titlesort, tr_albumsort, tr_artistsort, \
                            tr_albumartistsort, tr_composersort = crow

                        # process the track

                        if pl_change == 'I':
                        
                            # is an insert
                            # insert master and create audit records
                            try:
                                pl = (pltitle,
                                      plid, 
                                      plfile, trackfile, 
                                      ploccurs, pltrack, tr_id, 0, 
                                      tr_inserted, tr_created, tr_lastmodified,
                                      plfilecreated, plfilelastmodified, 
                                      trackfilecreated, trackfilelastmodified, 
                                      scannumber, lastscanned)
                                logstring = "playlist track inserted: %s : %s" % (plfile, trackfile)
                                filelog.write_log(logstring)
                                logstring = "INSERT: " + str(pl)
                                filelog.write_verbose_log(logstring)
                                c.execute("""insert into playlists values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", pl)
                                # pre                                
                                ipl = clearpl(pl)
                                ipl += (0, 'I')
                                c.execute("""insert into playlists_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", ipl)
                                # post
                                ipl = pl + (1, 'I')
                                c.execute("""insert into playlists_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", ipl)
                            except sqlite3.Error, e:
                                errorstring = "Error inserting playlist track details: %s" % e.args[0]
                                filelog.write_error(errorstring)

                        else:

                            # is an update
                            # create audit records and update master
                            try:
                                # get existing record, must be found as we got it earlier
                                c.execute("""select * from playlists where playlist=? and plfile=? and trackfile=? and occurs=?""",
                                            (pltitle, plfile, trackfile, ploccurs))
                                row = c.fetchone()
                                pl_playlist, pl_plid, pl_plfile, pl_trackfile, pl_occurs, pl_track, pl_track_id, pl_track_rowid, pl_inserted, pl_created, pl_lastmodified, pl_plfilecreated, pl_plfilelastmodified, pl_trackfilecreated, pl_trackfilelastmodified, pl_scannumber, pl_lastscanned = row
                            except sqlite3.Error, e:
                                errorstring = "Error getting playlist details: %s" % e.args[0]
                                filelog.write_error(errorstring)

                            # check if only the playlist/track timestamps have changed
                            # - if so we don't need to create audit records as those changes are not
                            #   propagated into tracks

                            if int(pl_track) == pltrack and \
                               pl_plfilecreated == plfilecreated and \
                               pl_plfilelastmodified == plfilelastmodified and \
                               pl_trackfilecreated == trackfilecreated and \
                               pl_trackfilelastmodified == trackfilelastmodified:

                                try:
                                    pl = (plfilecreated, plfilelastmodified, 
                                          trackfilecreated, trackfilelastmodified, 
                                          scannumber, lastscanned,
                                          pltitle, plfile, trackfile, ploccurs)
                                    logstring = "UPDATE: " + str(pl)
                                    filelog.write_verbose_log(logstring)
                                    c.execute("""update playlists set
                                                 plfilecreated=?, plfilelastmodified=?, 
                                                 trackfilecreated=?, trackfilelastmodified=?, 
                                                 scannumber=?, lastscanned=?
                                                 where playlist=? and plfile=? and trackfile=? and occurs=?""", 
                                                 pl)
                                except sqlite3.Error, e:
                                    errorstring = "Error updating playlist track details: %s" % e.args[0]
                                    filelog.write_error(errorstring)

                            else:

                                # create audit records and update master
                                try:
                                    # pre                                
                                    pl = (pl_playlist, 
                                          pl_plid,
                                          pl_plfile, pl_trackfile, 
                                          pl_occurs, pl_track, pl_track_id, 0,
                                          pl_inserted, pl_created, pl_lastmodified, 
                                          pl_plfilecreated, pl_plfilelastmodified, 
                                          pl_trackfilecreated, pl_trackfilelastmodified, 
                                          scannumber, pl_lastscanned)
                                    plu = pl + (0, 'U')
                                    c.execute("""insert into playlists_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", plu)
                                    # post
                                    pl = (pltitle,
                                          plid,
                                          plfile, trackfile, 
                                          ploccurs, pltrack, pl_track_id, 0,
                                          pl_inserted, pl_created, pl_lastmodified,
                                          plfilecreated, plfilelastmodified, 
                                          trackfilecreated, trackfilelastmodified, 
                                          scannumber, lastscanned)
                                    plu = pl + (1, 'U')
                                    c.execute("""insert into playlists_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", plu)
                                    # now update the existing record
                                    pl = (pltrack, pl_track_id, 
                                          pl_inserted, pl_created, pl_lastmodified,
                                          plfilecreated, plfilelastmodified, 
                                          trackfilecreated, trackfilelastmodified, 
                                          scannumber, lastscanned,
                                          pltitle, plfile, trackfile, ploccurs)
                                    logstring = "Existing playlist track updated: %s" % (trackfile)
                                    filelog.write_log(logstring)
                                    logstring = "UPDATE: " + str(pl)
                                    filelog.write_verbose_log(logstring)
                                    c.execute("""update playlists set
                                                 track=?, track_id=?, 
                                                 inserted=?, created=?, lastmodified=?,
                                                 plfilecreated=?, plfilelastmodified=?, 
                                                 trackfilecreated=?, trackfilelastmodified=?, 
                                                 scannumber=?, lastscanned=?
                                                 where playlist=? and plfile=? and trackfile=? and occurs=?""", 
                                                 pl)
                                except sqlite3.Error, e:
                                    errorstring = "Error updating playlist track details: %s" % e.args[0]
                                    filelog.write_error(errorstring)

            except KeyboardInterrupt: 
                raise

    db.commit()

    # now look for playlist entries for this path that we didn't encounter - they must have been deleted or moved so flag for deletion
    # we use the playlist filespec to compare against (it's saved against each file in the set)
    try:
        scanpathlike = "%s%s" % (scanpath, '%')
        c2.execute("""select * from playlists where scannumber != ? and plfile like ?""",
                    (scannumber, scanpathlike))
        for crow in c2:
            lastscanned = time.time()
            # get data
            pl_playlist, pl_plid, pl_plfile, pl_trackfile, pl_occurs, pl_track, pl_track_id, pl_track_rowid, pl_inserted, pl_created, pl_lastmodified, pl_plfilecreated, pl_plfilelastmodified, pl_trackfilecreated, pl_trackfilelastmodified, pl_scannumber, pl_lastscanned = crow
            # check if we have matched a partial path
            if scanpath != pl_plfile:
                if pl_plfile[len(scanpath)] != os.sep:
                    continue
            # create audit records

            pl = (pl_playlist,
                  pl_plid,
                  pl_plfile, pl_trackfile, 
                  pl_occurs, pl_track, pl_track_id, 
                  pl_track_rowid,
                  pl_inserted, pl_created, pl_lastmodified, 
                  pl_plfilecreated, pl_plfilelastmodified, 
                  pl_trackfilecreated, pl_trackfilelastmodified, 
                  scannumber, pl_lastscanned)
            # pre
            pld = pl + (0, 'D')
            c.execute("""insert into playlists_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", pld)
            # post
            pld = clearpl(pl, lastscanned=lastscanned)
            pld += (1, 'D')
            c.execute("""insert into playlists_update values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", pld)
            # delete record from tags
            logstring = "Existing playlist track not found: %s, %s" % (pl_plfile, pl_trackfile)
            filelog.write_log(logstring)
            logstring = "DELETE: " + str(pl)
            filelog.write_verbose_log(logstring)
            c.execute("""delete from playlists where playlist=? and plfile=? and trackfile=? and occurs=?""", (pl_playlist, pl_plfile, pl_trackfile, pl_occurs))

    except sqlite3.Error, e:
        errorstring = "Error processing playlist track deletions: %s" % e.args[0]
        filelog.write_error(errorstring)


    # TODO: we don't process playlists in movetags, so here we're removing redundant audit records
    # - we need to remove their creation when this is finalised (and remove this code)
    try:
        c.execute("""delete from playlists_update""")
    except sqlite3.Error, e:
        errorstring = "Error deleting playlists_update entries: %s" % e.args[0]
        filelog.write_error(errorstring)

#    db2.commit()
    db.commit()

    # update stats
    try:
        c.execute("""analyze""")
    except sqlite3.Error, e:
        errorstring = "Error updating stats: %s" % e.args[0]
        filelog.write_error(errorstring)

    db.commit()

    # complete
    c.close()
    c2.close()

def get_art_id(c, artspec):

    # get unique id for album art
    artid = None
    try:
        c.execute("""select id, artpath from art where artpath=?""",
                    (artspec, ))
        row = c.fetchone()
        if row:
            artid, artpath = row
        else:
            c.execute('''insert into art values (?,?)''', (None, artspec))
            artid = c.lastrowid
    except sqlite3.Error, e:
        errorstring = "Error checking/inserting art: %s" % e.args[0]
        filelog.write_error(errorstring)
    return artid

def get_workvirtual_update(scannumber):

    # get tag records that have been changed and find all associated workvirtuals
    try:
        statement = """select distinct(wvfile) from workvirtuals where trackfile in (select distinct(path || "%s" || filename) from tags_update where scannumber=?)""" % (os.sep)
        c.execute(statement, (scannumber, ))
        for crow in c:
            wvfile, = crow
            path, spec = os.path.split(wvfile)
            yield path, ['..wv..'], [spec]
            
    except sqlite3.Error, e:
        errorstring = "Error processing track changes against workvirtuals: %s" % e.args[0]
        filelog.write_error(errorstring)

def get_playlist_update(scannumber):

    # get tag records that have been changed and find all associated playlists
    try:
        statement = """select distinct(plfile) from playlists where trackfile in (select distinct(path || "%s" || filename) from tags_update where scannumber=?)""" % (os.sep)
        c.execute(statement, (scannumber, ))
        for crow in c:
            plfile, = crow
            path, spec = os.path.split(plfile)
            yield path, ['..pl..'], [spec]
            
    except sqlite3.Error, e:
        errorstring = "Error processing track changes against playlists: %s" % e.args[0]
        filelog.write_error(errorstring)

def generate_subset(options, sourcedatabase, targetdatabase, where):

    db = sqlite3.connect(targetdatabase)
    db.execute("PRAGMA synchronous = 0;")
    c = db.cursor()

    if sourcedatabase != targetdatabase:

        logstring = "Extracting tag data"
        filelog.write_log(logstring)
        # we are generating a new database, so attach to old and extract the data we need
        c.execute("attach ? as old", (sourcedatabase, ))
        # copy selected tags into new database
        statement = """insert into tags select * from old.tags %s""" % where
        logstring = statement
        filelog.write_log(logstring)
        c.execute(statement) 
        # copy related workvirtuals into new database
        statement = """insert into workvirtuals select * from old.workvirtuals where id in (select id from tags)"""
        logstring = statement
        filelog.write_log(logstring)
        c.execute(statement) 
        # copy associated art
        statement = """insert into art select distinct old.art.id, old.art.artpath from old.art, tags where tags.folderartid = old.art.id or tags.trackartid = old.art.id"""
        c.execute(statement) 
        logstring = "Tag data extracted"
        filelog.write_log(logstring)

    logstring = "Generating change data"
    filelog.write_log(logstring)

    # create new record for this scan
    c.execute('''insert into scans values (?,?)''', (None, "generated"))
    scannumber = c.lastrowid
    logstring = "Scannumber: %d" % scannumber
    filelog.write_log(logstring)

    if options.regenerate:
        # delete any old outstanding scan records
        c.execute('''delete from scans where id < ?''', (scannumber, ))

    lastscanned = time.time()

    # create audit records for tags
    try:
        statement = """insert into tags_update 
                       select id, '',
                       '', '', '',
                       '', '', '',
                       '', '', '', 
                       '', '', 
                       '', path, filename, 
                       '', '', 
                       '', '', 
                       '', '',
                       '', '', '', 
                       '', '%s',
                       '', '', 
                       '', '%f',
                       '', '', '', 
                       '', '',
                       '0', 'I' from tags""" % (scannumber, lastscanned)
        c.execute(statement) 
        statement = """insert into tags_update 
                       select id, id2,
                       title, artist, album,
                       genre, track, year,
                       albumartist, composer, codec, 
                       length, size, 
                       created, path, filename, 
                       discnumber, comment, 
                       folderart, trackart, 
                       bitrate, samplerate, 
                       bitspersample, channels, mime, 
                       lastmodified, '%s',
                       folderartid, trackartid, 
                       inserted, '%f', 
                       titlesort, albumsort, artistsort, 
                       albumartistsort, composersort,
                       '1', 'I' from tags""" % (scannumber, lastscanned)
        c.execute(statement) 
    except sqlite3.Error, e:
        errorstring = "Error generating tag_updates: %s" % e.args[0]
        filelog.write_error(errorstring)

    # create audit records for workvirtuals
    try:
        statement = """insert into workvirtuals_update 
                       select title, 
                       wvfile, plfile, trackfile,
                       occurs,
                       '', '', '', 
                       '', '', '',
                       '', '',
                       type, id,
                       '', '', '',
                       '', '',
                       '', '', 
                       '', '', 
                       '%s', '%f',
                       '', '', '', 
                       '', '',
                       '',
                       '0', 'I' from workvirtuals""" % (scannumber, lastscanned)
        c.execute(statement) 
        statement = """insert into workvirtuals_update 
                       select title,
                       wvfile, plfile, trackfile, 
                       occurs, 
                       artist, albumartist, composer,
                       year, track, genre,
                       cover, discnumber,
                       type, id,
                       inserted, created, lastmodified,
                       wvfilecreated, wvfilelastmodified,
                       plfilecreated, plfilelastmodified,
                       trackfilecreated, trackfilelastmodified,
                       '%s', '%f',
                       titlesort, albumsort, artistsort, 
                       albumartistsort, composersort,
                       coverartid,
                       '1', 'I' from workvirtuals""" % (scannumber, lastscanned)
        c.execute(statement) 
    except sqlite3.Error, e:
        errorstring = "Error generating workvirtual_updates: %s" % e.args[0]
        filelog.write_error(errorstring)

    db.commit()
    c.close()

    logstring = "Change data generated"
    filelog.write_log(logstring)

def cleartags(tags, lastscanned=''):
    id, id2, \
    title, artist, album, \
    genre, track, year, \
    albumartist, composer, codec, \
    length, size, \
    created, path, filename, \
    discnumber, comment, \
    folderart, trackart, \
    bitrate, samplerate, \
    bitspersample, channels, mime, \
    lastmodified, scannumber, \
    folderartid, trackartid, \
    inserted, o_lastscanned, \
    titlesort, albumsort, artistsort, \
    albumartistsort, composersort = tags
    tags = (id, '',
            '', '', '',
            '', '', '',
            '', '', '', 
            '', '', 
            '', path, filename, 
            '', '', 
            '', '', 
            '', '',
            '', '', '', 
            '', scannumber,
            '', '',
            '', lastscanned,
            '', '', '', 
            '', '')
    return tags

def clearwv(wv, lastscanned=''):
    wvtitle, \
    wvfile, plfile, trackfile, \
    wvoccurs, \
    wvartist, wvalbumartist, wvcomposer, \
    wvyear, wvtrack, wvgenre, \
    wvcover, wvdiscnumber, \
    wvtype,  wvid, \
    wvinserted, wvcreated, wvlastmodified, \
    wvfilecreated, wvfilelastmodified, \
    plfilecreated, plfilelastmodified, \
    trackfilecreated, trackfilelastmodified, \
    scannumber, lastscanned, \
    titlesort, albumsort, artistsort, \
    albumartistsort, composersort, \
    coverartid = wv
    wv = (wvtitle, 
          wvfile, plfile, trackfile,
          wvoccurs,
          '', '', '', 
          '', '', '',
          '', '',
          wvtype, wvid,
          '', '', '',
          '', '',
          '', '', 
          '', '', 
          scannumber, lastscanned,
          '', '', '', 
          '', '',
          '')
    return wv

def clearpl(pl, lastscanned=''):
    pltitle, \
    plid, \
    plfile, trackfile, \
    ploccurs, pltrack, plid, plrowid, \
    plinserted, plcreated, pllastmodified, \
    plfilecreated, plfilelastmodified, \
    trackfilecreated, trackfilelastmodified, \
    scannumber, lastscanned = pl
    pl = (pltitle, 
          plid,
          plfile, trackfile,
          ploccurs, '', plid, plrowid,
          '', '', '',
          '', '',
          '', '', 
          scannumber, lastscanned)
    return pl

def encodeunicode(data):
    if isinstance(data, str):
        return unicode(data)
    if isinstance(data, ASFUnicodeAttribute):   # hack for issue with asf type in mutagen
        return data.__str__()
    elif isinstance(data, dict):
        return dict(map(encodeunicode, data.iteritems()))
    elif isinstance(data, (list, tuple, set, frozenset)):
        return type(data)(map(encodeunicode, data))
    else:
        return data

def get_folderart(files):
    '''
        check through files found for folderart
    '''
    flist = {}
    for f in files:
        ff, ex = os.path.splitext(f.lower())
        if ex in artextns:
            if ff == 'folder' or ff == 'cover':
                flist[ff] = f
            elif ff.startswith('albumart') and ff.endswith('large'):
                flist['albumart'] = f
            elif ff.endswith('front'):
                flist['front'] = f
    if not flist:
        return None
    else:
        if 'folder' in flist.keys():
            return flist['folder']
        elif 'cover' in flist.keys():
            return flist['cover']
        elif 'albumart' in flist.keys():
            return flist['albumart']
        elif 'front' in flist.keys():
            return flist['front']

def getfilestat(filespec):
    try:
        fstat = os.stat(filespec)
        directory = os.path.isdir(filespec)
    except OSError:
        return False, None, None, None, False
    fsize = unicode(fstat.st_size)
    fctime = unicode(fstat.st_ctime)
    fmtime = unicode(fstat.st_mtime)
    fatime = unicode(fstat.st_atime)
    if linux_file_modification_time == 'ctime' and os.name != 'nt':
        created = fmtime
        lastmodified = fctime
    elif linux_file_creation_time == 'atime' and os.name != 'nt':
        created = fatime
        lastmodified = fmtime
    elif os.name == 'nt':
        created = fctime
        lastmodified = fmtime
    elif os.name != 'nt':
        created = ''
        lastmodified = fmtime
 
    return True, created, lastmodified, fsize, directory

workvirtualkeys = {
    'type=': 'wvtype',
    'title=': 'wvtitle',
    'album=': 'wvtitle',    # added for .tags
    'artist=': 'wvartist',
    'albumartist=': 'wvalbumartist',
    'composer=': 'wvcomposer',
    'year=': 'wvyear',
    'genre=': 'wvgenre',
    'cover=': 'wvcover',
    'discnumber=': 'wvdiscnumber',
    'inserted=': 'wvinserted',
    'created=': 'wvcreated',
    'lastmodified=': 'wvlastmodified',
    'titlesort=': 'wvtitlesort',
    'albumsort=': 'wvalbumsort',
    'artistsort=': 'wvartistsort',
    'albumartistsort=': 'wvalbumartistsort',
    'composersort=': 'wvcomposersort',
    }

m3u_playlist_extensions = ['.m3u', '.m3u8']
pls_playlist_extensions = ['.pls']
wpl_playlist_extensions = ['.wpl']
playlist_extensions = m3u_playlist_extensions + pls_playlist_extensions + wpl_playlist_extensions

def read_workvirtualfile(wvfilespec, wvextension, wvfilepath, database):
    '''
        read a file containing works and virtuals
        and return a list of work/virtual records
    '''
    if wvextension == tags_file_extension:
        wvtype = 'tags'
    else:
        exttype = work_virtual_extensions[wvextension]
        if exttype == 'workvirtual':
            wvtype = 'virtual'
        elif exttype == 'work':
            wvtype = 'work'
        elif exttype == 'virtual':
            wvtype = 'virtual'
    default_wvtype = wvtype
#    wvtitle = wvartist = wvalbumartist = wvcomposer = wvyear = wvgenre = wvcover = wvdiscnumber = wvinserted = wvcreated = wvlastmodified = wvtitlesort = wvalbumsort = wvartistsort = wvalbumartistsort = wvcomposersort = tracktitle = tracklength = None
    tracks = []
    trackcounts = defaultdict(int)
    success, wvfilecreated, wvfilelastmodified, wvfilefsize, filler = getfilestat(wvfilespec)
    wvcount = 0
    newkeyset = True
    for line in codecs.open(wvfilespec,'r','utf-8'):
        if line == '': continue
        if line.startswith('#'): continue
        if line.strip() == '': continue
        keyfound = False
        for key in workvirtualkeys:
            if line.lower().startswith(key):

                if newkeyset:
                    wvtitle = wvartist = wvalbumartist = wvcomposer = wvyear = wvgenre = wvcover = wvdiscnumber = wvinserted = wvcreated = wvlastmodified = wvtitlesort = wvalbumsort = wvartistsort = wvalbumartistsort = wvcomposersort = tracktitle = tracklength = None
                    wvtracknumber = 0
                    wvtype = default_wvtype
                    newkeyset = False                    
                value = get_key_value(key, line)
                if key == 'type=':
                    if not (value == 'work' or value == 'virtual'): value = wvtype
                    wvtype = None
                if key == 'cover=' and value != '': value = checkpath(value, wvfilepath)
                if eval("%s==None" % workvirtualkeys[key]):
                    exec('%s=u"%s"' % (workvirtualkeys[key], value))
                else:
                    exec('%s+=u"%s"' % (workvirtualkeys[key], '\\n'))
                    exec('%s+=u"%s"' % (workvirtualkeys[key], value))
                if key == 'title=' or key == 'album=': wvcount += 1
                keyfound = True
                break
        if not keyfound:
            
            if line.lower().startswith('tracktitle='):
                tracktitle = get_key_value('tracktitle=', line)
                continue
            if line.lower().startswith('tracklength='):
                tracklength = get_key_value('tracklength=', line)
                continue
                        
            newkeyset = True
            filespec = line.strip()
            filespec = checkpath(filespec, wvfilepath)
            for filespec in generate_workvirtualfile_record(filespec, database):
                for trackcountdata, trackdata in process_workvirtualfile_file(filespec, wvfilepath, wvtype):
#                    print "============="
#                    print trackdata
                    if trackdata:
                        wvtracknumber += 1
                        ftrack = trackcountdata
                        filespec, created, lastmodified, trackspec, trackcreated, tracklastmodified = trackdata
                        trackdata = (wvcount, wvfilespec, wvfilecreated, wvfilelastmodified, filespec, created, lastmodified, trackspec, trackcreated, tracklastmodified, wvtype, wvtitle, wvartist, wvalbumartist, wvcomposer, wvyear, wvgenre, wvcover, wvdiscnumber, trackcounts[(wvtitle, ftrack)], wvinserted, wvcreated, wvlastmodified, wvtitlesort, wvalbumsort, wvartistsort, wvalbumartistsort, wvcomposersort, tracktitle, tracklength, wvtracknumber)
                        trackcounts[(wvtitle, ftrack)] += 1    
                        tracks.append(trackdata)
    return tracks

def get_key_value(key, line):
    value = line[len(key):]
    value = value.replace('"', '\\"')
    if value.endswith('\n'): value = value[:-1]
    if value.endswith('\r'): value = value[:-1]
    return value

def read_playlistfile(filespec, filepath):
    '''
        read a file containing tracks
        and return a list of track records
    '''
    tracks = []
    trackcounts = defaultdict(int)
    pltrack = 1
    for trackcountdata, trackdata in process_workvirtualfile_file(filespec, filepath, 'playlist'):
#        print "============="
#        print trackdata
        if trackdata:
            ftrack = trackcountdata
            filespec, created, lastmodified, trackspec, trackcreated, tracklastmodified = trackdata
            trackdata = (filespec, created, lastmodified, trackspec, trackcreated, tracklastmodified, pltrack, trackcounts[ftrack])
            trackcounts[ftrack] += 1    
            tracks.append(trackdata)
            pltrack += 1
    return tracks

def generate_workvirtualfile_record(filespec, database):
    filelist = []
    directory = os.path.isdir(filespec)
    if directory:
    
        visitedpaths = []
        for filepath, dirs, files in os.walk(filespec, followlinks=follow_symlinks):

            filepath = os.path.abspath(os.path.realpath(filepath))
            if follow_symlinks:
                if filepath in visitedpaths:
                    errorstring = "Path already visited, check symlinks: %s" % filepath
                    filelog.write_error(errorstring)
                    exit(1)
                visitedpaths.append(filepath)

            if type(filepath) == 'str': filepath = filepath.decode(enc, 'replace')
            if type(dirs) == 'str': dirs = [d.decode(enc, 'replace') for d in dirs]
            if type(files) == 'str': files = [f.decode(enc, 'replace') for f in files]

            files.sort()
            for fn in files:
                if check_workvirtual_file(fn):
                    album, discnumber, tracknumber = get_workvirtual_track_details(filepath, fn)
                    filelist.append((fn, filepath, album, discnumber, tracknumber))
        # sort the list on album/discnumber/tracknumber
        filelist = sorted(filelist, key=itemgetter(2,3,4))
        for entry in filelist:
            fn, filepath, album, discnumber, tracknumber = entry
            filespec = os.path.join(filepath, fn)
            yield filespec
    else:
        if check_workvirtual_file(filespec):
            yield filespec

def check_workvirtual_file(filespec):
    filename = os.path.basename(filespec)
    ff, ex = os.path.splitext(filename)
    process = True
    if filename.lower() in file_name_exclusions: process = False
    elif ex.lower() in artextns: process = False
    elif ex.lower() in file_extn_exclusions: process = False
    return process

def process_workvirtualfile_file(filespec, wvfilepath, wvtype):
    ff, ex = os.path.splitext(filespec.lower())
    if ex in playlist_extensions:
        success, plcreated, pllastmodified, plfsize, filler = getfilestat(filespec)
        if not success:
            # this playlist does not exist, reject the work/virtual line
            errorstring = "Error processing %s: %s : playlist does not exist" % (wvtype, filespec)
            filelog.write_error(errorstring)
            yield None, None
            return
        pltracks = read_playlist(filespec, ex)
        for pltrack in pltracks:
            pltrack = checkpath(pltrack, wvfilepath)
            if is_stream(pltrack) and wvtype == 'playlist':
                trackcreated = tracklastmodified = None
            else:    
                success, trackcreated, tracklastmodified, trackfsize, filler = getfilestat(pltrack)
                if not success:
                    # this playlist track does not exist, reject the playlist line
                    errorstring = "Error processing %s: %s : playlist track does not exist" % (wvtype, pltrack)
                    filelog.write_error(errorstring)
                    continue
            trackcountdata = pltrack
            trackdata = (filespec, plcreated, pllastmodified, pltrack, trackcreated, tracklastmodified)
            yield trackcountdata, trackdata
    else:
        if ex in work_virtual_extensions:
            # we don't support nested workvirtuals, reject the work/virtual line
            errorstring = "Error processing %s: %s : nested workvirtuals are not supported" % (wvtype, filespec)
            filelog.write_error(errorstring)
            yield None, None
            return
        filespec = checkpath(filespec, wvfilepath)
        success, trackcreated, tracklastmodified, trackfsize, directory = getfilestat(filespec)
        if not success:
            # this track does not exist, reject the work/virtual line
            errorstring = "Error processing %s: %s : track does not exist" % (wvtype, filespec)
            filelog.write_error(errorstring)
            yield None, None
            return
        trackcountdata = filespec
        trackdata = ('', '', '', filespec, trackcreated, tracklastmodified)
        yield trackcountdata, trackdata

def read_playlist(filespec, extension):
    if extension in m3u_playlist_extensions:
        return read_m3u_playlist(filespec)
    if extension in pls_playlist_extensions:
        return read_pls_playlist(filespec)
    if extension in wpl_playlist_extensions:
        return read_wpl_playlist(filespec)

def read_m3u_playlist(filespec):
    tracks = []
    for line in codecs.open(filespec,'r','utf-8'):
        if line == '': continue
        if line.startswith('#'): continue
        if line.strip() == '': continue
        filespec = line.strip()
        if check_workvirtual_file(filespec):
            ff, ex = os.path.splitext(filespec.lower())
            if ex in playlist_extensions:
                # we don't support nested playlists, reject the playlist line
                errorstring = "Error processing playlist: %s : nested playlists are not supported" % (filespec)
                filelog.write_error(errorstring)
                continue
            tracks.append(filespec)
    return tracks

def read_pls_playlist(filespec):
    tracks = []
    for line in codecs.open(filespec,'r','utf-8'):
        # Note - currently only looks for 'fileN=' lines (so ignores 'titleN=')
        line = line.strip()
        if re.match('file[0-9]+=', line, re.I):
            filespec = line[line.find('=')+1:]
            if check_workvirtual_file(filespec):
                ff, ex = os.path.splitext(filespec.lower())
                if ex in playlist_extensions:
                    # we don't support nested playlists, reject the playlist line
                    errorstring = "Error processing playlist: %s : nested playlists are not supported" % (filespec)
                    filelog.write_error(errorstring)
                    continue
                tracks.append(filespec)
    return tracks

def read_wpl_playlist(filespec):
    tracks = []
    for line in codecs.open(filespec,'r','utf-8'):
        # Note - currently only looks for '<media src="' lines (so ignores '<title>')
        # TODO: consider XML reader
        line = line.strip()
        if re.match('<media[\s]+src=["\']', line, re.I):
            quotepos = line.find('=') + 1
            quotechar = line[quotepos:quotepos+1]
            quotepos2 = line.rfind(quotechar)
            if quotepos2 > quotepos + 1:
                filespec = line[quotepos+1:quotepos2]
                if check_workvirtual_file(filespec):
                    ff, ex = os.path.splitext(filespec.lower())
                    if ex in playlist_extensions:
                        # we don't support nested playlists, reject the playlist line
                        errorstring = "Error processing playlist: %s : nested playlists are not supported" % (filespec)
                        filelog.write_error(errorstring)
                        continue
                    tracks.append(filespec)
    return tracks

def get_workvirtual_track_details(trackpath, trackfile):

    try:
        c.execute("""select album, discnumber, track from tags where path=? and filename=?""", (trackpath, trackfile))
    except sqlite3.Error, e:
        errorstring = "Error getting tags for workvirtual track details: %s" % e.args[0]
        filelog.write_error(errorstring)
    crow = c.fetchone()
    if crow:
        # track exists, get data
        album, discnumber, track = crow
        track = adjust_tracknumber(track)
        discnumber = truncate_number(discnumber)
        crow = (album, discnumber, track)
    else:
        crow = (None, None, None)
    return crow

paths = ['http://', 'file://', 'rtsp://', 'smb://', 'rtp://', 'ftp://', 'mms://']

def checkpath(pathspec, wvfilepath):
    for p in paths:
        if pathspec.startswith(p): return pathspec
    if not os.path.isabs(pathspec):
        pathspec = os.path.abspath(os.path.join(wvfilepath, pathspec))
    return pathspec

def is_stream(filespec):
    for p in paths:
        if filespec.startswith(p):
            return True
    return False

def checktag(wvtag, tag):
    if not wvtag:
        return tag
    elif wvtag.lower() == '<blank>':
        return ''
    elif wvtag:
        return wvtag
    else:
        return tag

def check_database_exists(database):
    ''' 
        create database if it doesn't already exist
        if it exists, create table if it doesn't exist
        return abs path
    '''
    if not os.path.isabs(database):
        database = os.path.join(os.getcwd(), database)
    create_database(database)
    return database

def create_database(database):
    db = sqlite3.connect(database)
    c = db.cursor()
    try:
        # scans - unique id for each scan
        c.execute('SELECT count(*) FROM sqlite_master WHERE type="table" AND name="scans"')
        n, = c.fetchone()
        if n == 0:
            c.execute('''create table scans (id integer primary key autoincrement,
                                             scanpath text)
                      ''')
            c.execute('''create unique index inxScans on scans (id)''')
    
        # art - unique id for each piece of album art
        c.execute('SELECT count(*) FROM sqlite_master WHERE type="table" AND name="art"')
        n, = c.fetchone()
        if n == 0:
            c.execute('''create table art (id integer primary key autoincrement,
                                           artpath text)
                      ''')
            c.execute('''create unique index inxArt on art (id)''')
            c.execute('''create unique index inxArtArtpath on art (artpath)''')
    
        # tags - contain all detail from tags
        c.execute('SELECT count(*) FROM sqlite_master WHERE type="table" AND name="tags"')
        n, = c.fetchone()
        if n == 0:
            c.execute('''create table tags (id text, id2 text,
                                            title text, artist text, album text,
                                            genre text, track text, year text,
                                            albumartist text, composer text, codec text,
                                            length text, size text,
                                            created text, path text, filename text,
                                            discnumber text, comment text, 
                                            folderart text, trackart text,
                                            bitrate text, samplerate text, 
                                            bitspersample text, channels text, mime text,
                                            lastmodified text,
                                            scannumber integer, folderartid text, trackartid text,
                                            inserted text, lastscanned text,
                                            titlesort text, albumsort text, artistsort text, 
                                            albumartistsort text, composersort text)
                      ''')
            c.execute('''create unique index inxTagsPathFile on tags (path, filename)''')
            c.execute('''create unique index inxTags on tags (id)''')
            c.execute('''create index inxTagsScannumber on tags (scannumber)''')

        # tags_update - pre and post data from tags around an update
        c.execute('SELECT count(*) FROM sqlite_master WHERE type="table" AND name="tags_update"')
        n, = c.fetchone()
        if n == 0:
            c.execute('''create table tags_update (id text, id2 text,
                                                   title text, artist text, album text,
                                                   genre text, track text, year text,
                                                   albumartist text, composer text, codec text,
                                                   length text, size text,
                                                   created text, path text, filename text,
                                                   discnumber text, comment text, 
                                                   folderart text, trackart text,
                                                   bitrate text, samplerate text, 
                                                   bitspersample text, channels text, mime text,
                                                   lastmodified text,
                                                   scannumber integer, folderartid text, trackartid text,
                                                   inserted text, lastscanned text,
                                                   titlesort text, albumsort text, artistsort text, 
                                                   albumartistsort text, composersort text,
                                                   updateorder integer, updatetype text)
                      ''')
#            c.execute('''create unique index inxTagsUpdatePathFile on tags_update (path, filename, scannumber)''')
            c.execute('''create unique index inxTagsUpdateIdScanUpdate on tags_update (id, scannumber, updateorder)''')
            c.execute('''create unique index inxTagsUpdateScanUpdateId on tags_update (scannumber, updatetype, id, updateorder)''')
#            c.execute('''create index inxTagsUpdate on tags_update (id)''')
            c.execute('''create index inxTagsUpdateScannumber on tags_update (scannumber)''')

        # workvirtuals - contain all detail from workvirtual records
        c.execute('SELECT count(*) FROM sqlite_master WHERE type="table" AND name="workvirtuals"')
        n, = c.fetchone()
        if n == 0:
            c.execute('''create table workvirtuals (title text, 
                                                    wvfile text, plfile text, trackfile text, 
                                                    occurs text, 
                                                    artist text, albumartist text, composer text,
                                                    year text, track text, genre text,
                                                    cover text, discnumber text,
                                                    type text, id text,
                                                    inserted text, created text, lastmodified text,
                                                    wvfilecreated text, wvfilelastmodified text,
                                                    plfilecreated text, plfilelastmodified text,
                                                    trackfilecreated text, trackfilelastmodified text,
                                                    scannumber integer, lastscanned text,
                                                    titlesort text, albumsort text, artistsort text, 
                                                    albumartistsort text, composersort text,
                                                    coverartid integer)
                      ''')
            c.execute('''create unique index inxWorkvirtualFile on workvirtuals (title, wvfile, plfile, trackfile, occurs)''')
            c.execute('''create index inxWorkvirtualScannumber on workvirtuals (scannumber)''')

        # workvirtuals_update - pre and post data from workvirtuals around an update
        c.execute('SELECT count(*) FROM sqlite_master WHERE type="table" AND name="workvirtuals_update"')
        n, = c.fetchone()
        if n == 0:
            c.execute('''create table workvirtuals_update (title text,
                                                           wvfile text, plfile text, trackfile text, 
                                                           occurs text, 
                                                           artist text, albumartist text, composer text,
                                                           year text, track text, genre text,
                                                           cover text, discnumber text,
                                                           type text, id text,
                                                           inserted text, created text, lastmodified text,
                                                           wvfilecreated text, wvfilelastmodified text,
                                                           plfilecreated text, plfilelastmodified text,
                                                           trackfilecreated text, trackfilelastmodified text,
                                                           scannumber integer, lastscanned text,
                                                           titlesort text, albumsort text, artistsort text, 
                                                           albumartistsort text, composersort text,
                                                           coverartid integer,
                                                           updateorder integer, updatetype text)
                      ''')
            c.execute('''create unique index inxWorkvirtualUpdateIdScanUpdate on workvirtuals_update (title, wvfile, plfile, trackfile, occurs, scannumber, updateorder)''')
            c.execute('''create unique index inxWorkvirtualUpdateScanUpdateId on workvirtuals_update (scannumber, updatetype, title, wvfile, plfile, trackfile, occurs, updateorder)''')
            c.execute('''create index inxWorkvirtualUpdateScannumber on workvirtuals_update (scannumber)''')

        # playlists
        c.execute('SELECT count(*) FROM sqlite_master WHERE type="table" AND name="playlists"')
        n, = c.fetchone()
        if n == 0:
            c.execute('''create table playlists (playlist text COLLATE NOCASE,
                                                 id text,
                                                 plfile text, trackfile text, 
                                                 occurs text, track text, track_id text,
                                                 track_rowid integer,
                                                 inserted text, created text, lastmodified text,
                                                 plfilecreated text, plfilelastmodified text,
                                                 trackfilecreated text, trackfilelastmodified text,
                                                 scannumber integer, lastscanned text)
                      ''')
            c.execute('''create unique index inxPlaylistTrackFiles on playlists (playlist, plfile, trackfile, occurs)''')
            c.execute('''create index inxPlaylists on playlists (playlist)''')
#            c.execute('''create index inxPlaylistFiles on playlists (plfile)''')
            c.execute('''create index inxPlaylistIDs on playlists (id)''')
            c.execute('''create index inxPlaylistsScannumber on playlists (scannumber)''')
            
        # playlists_update - pre and post data from playlists around an update
        c.execute('SELECT count(*) FROM sqlite_master WHERE type="table" AND name="playlists_update"')
        n, = c.fetchone()
        if n == 0:
            c.execute('''create table playlists_update (playlist text COLLATE NOCASE,
                                                        id text,
                                                        plfile text, trackfile text, 
                                                        occurs text, track text, track_id text,
                                                        track_rowid integer,
                                                        inserted text, created text, lastmodified text,
                                                        plfilecreated text, plfilelastmodified text,
                                                        trackfilecreated text, trackfilelastmodified text,
                                                        scannumber integer, lastscanned text,
                                                        updateorder integer, updatetype text)
                      ''')
            c.execute('''create unique index inxPlaylistUpdateIdScanUpdate on playlists_update (playlist, plfile, trackfile, occurs, scannumber, updateorder)''')
            c.execute('''create unique index inxPlaylistUpdateScanUpdateId on playlists_update (scannumber, updatetype, playlist, plfile, trackfile, occurs, updateorder)''')
            c.execute('''create index inxPlaylistUpdateScannumber on playlists_update (scannumber)''')

        # tagsfiles - contain file info for separate .tags files
        c.execute('SELECT count(*) FROM sqlite_master WHERE type="table" AND name="tagsfiles"')
        n, = c.fetchone()
        if n == 0:
            c.execute('''create table tagsfiles (path text, filename text, 
                                                 tracknumber text, tracktitle text, 
                                                 tracklength text, trackfile text,
                                                 artist text, album text, albumartist text, composer text,
                                                 year text, genre text,
                                                 cover text, coverartid text, 
                                                 discnumber text,
                                                 titlesort text, albumsort text, artistsort text, 
                                                 albumartistsort text, composersort text,
                                                 inserted text, created text, lastmodified text,
                                                 scannumber integer, lastscanned text)
                                                 ''')
            c.execute('''create unique index inxTagsfileFile on tagsfiles (trackfile)''')
            c.execute('''create index inxTagsfileScannumber on tagsfiles (scannumber)''')


    except sqlite3.Error, e:
        errorstring = "Error creating database: %s : %s" % (database, e)
        filelog.write_error(errorstring)
    db.commit()
    c.close()

def delete_updates(database):
    logstring = "Deleting outstanding updates"
    filelog.write_log(logstring)
    db = sqlite3.connect(database)
    c = db.cursor()
    try:
        c.execute('''delete from tags_update''')
    except sqlite3.Error, e:
        errorstring = "Error deleting from tags_update: %s : %s" % (table, e)
        filelog.write_error(errorstring)
    try:
        c.execute('''delete from workvirtuals_update''')
    except sqlite3.Error, e:
        errorstring = "Error deleting from workvirtuals_update: %s : %s" % (table, e)
        filelog.write_error(errorstring)
    try:
        c.execute('''delete from playlists_update''')
    except sqlite3.Error, e:
        errorstring = "Error deleting from playlists_update: %s : %s" % (table, e)
        filelog.write_error(errorstring)
    db.commit()
    c.close()
    logstring = "Outstanding updates deleted"
    filelog.write_log(logstring)

def reorganise(database):
    logstring = "Reorganising"
    filelog.write_log(logstring)
    db = sqlite3.connect(database)
    c = db.cursor()
    try:
        c.execute('''vacuum''')
    except sqlite3.Error, e:
        errorstring = "Error vacuuming: %s" % (e)
        filelog.write_error(errorstring)
    db.commit()
    c.close()
    logstring = "Reorganise complete"
    filelog.write_log(logstring)
    
def process_command_line(argv):
    """
        Return a 2-tuple: (settings object, args list).
        `argv` is a list of arguments, or `None` for ``sys.argv[1:]``.
    """
    if argv is None:
        argv = sys.argv[1:]

    # initialize parser object
    parser = optparse.OptionParser(
        formatter=optparse.TitledHelpFormatter(width=78),
        add_help_option=None)

    # options
    parser.add_option("-d", "--database", dest="database", type="string", 
                      help="write tags to DATABASE", action="store",
                      metavar="DATABASE")
    parser.add_option("-x", "--extract", dest="extract", type="string", 
                      help="write extract to DATABASE", action="store",
                      metavar="EXTRACT")
    parser.add_option("-w", "--where", dest="where", type="string", 
                      help="where clause to extract on", action="store",
                      metavar="WHERE")
    parser.add_option("-e", "--exclude", dest="exclude", type="string",
                      action="append", metavar="EXCLUDE",
                      help="exclude foldernames containing this string")
    parser.add_option("-r", "--regenerate",
                      action="store_true", dest="regenerate", default=False,
                      help="regenerate update records")
    parser.add_option("-q", "--quiet",
                      action="store_true", dest="quiet", default=False,
                      help="don't print status messages to stdout")
    parser.add_option("-v", "--verbose",
                      action="store_true", dest="verbose", default=False,
                      help="print verbose status messages to stdout")
#    parser.add_option("-c", "--ctime",
#                      action="store_true", dest="ctime", default=False,
#                      help="user ctime rather than mtime to detect file changes")
    parser.add_option('-h', '--help', action='help',
                      help='Show this help message and exit.')
    settings, args = parser.parse_args(argv)
    return settings, args

def main(argv=None):
    global lf
    options, args = process_command_line(argv)
    filelog.set_log_type(options.quiet, options.verbose)
    filelog.open_log_files()
    if not options.database:
        logstring = "Database must be specified"
        filelog.write_log(logstring)
    else:
        database = check_database_exists(options.database)
        if not options.quiet:
            logstring = "Database: %s" % database
            filelog.write_log(logstring)
        if options.regenerate:
            # NOTE: running empty_database here assumes the tracks data is in the same database as tags
            # (otherwise we'd have to pass -S to this module)
            empty_database(database)
            delete_updates(database)
            reorganise(database)
            generate_subset(options, database, database, '')
        elif options.extract and options.where:
            newdatabase = check_database_exists(options.extract)
            generate_subset(options, database, newdatabase, options.where)
        else:
            for path in args: 
                if path.endswith(os.sep): path = path[:-1]
                process_dir(path.decode(enc), options, database)
    filelog.close_log_files()
    return 0

if __name__ == "__main__":
    status = main()
    sys.exit(status)

