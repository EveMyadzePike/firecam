# Copyright 2020 Open Climate Tech Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""

This is the main code for reading images from webcams and detecting fires

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os, sys
from firecam.lib import settings
from firecam.lib import collect_args
from firecam.lib import goog_helper
from firecam.lib import img_archive

from firecam.lib import rect_to_squares
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' # quiet down tensorflow logging (must be done before tf_helper)
from firecam.lib import tf_helper
from firecam.lib import db_manager
from firecam.lib import email_helper
from firecam.lib import sms_helper
from firecam.lib import weather
from firecam.lib import rx_burns
from firecam.detection_policies import policies

import logging
import pathlib
import tempfile
import shutil
import time, datetime, dateutil.parser
import random
import math
import re
import json
import hashlib
import gc
import socket
from urllib.request import urlretrieve
import tensorflow as tf
from PIL import Image, ImageFile, ImageDraw, ImageFont
ImageFile.LOAD_TRUNCATED_IMAGES = True
import ffmpeg
from shapely.geometry import Polygon,Point


POST_DETECTION_UPDATE_MINS = 7 # minutes after detection to keep searching for new image frames for updated videos

def getNextImage(dbManager, cameras, stateless, counterName):
    """Gets the next image to check for smoke

    Uses a shared counter being updated by all cooperating detection processes
    to index into the list of cameras to download the image to a local
    temporary directory

    Args:
        dbManager (DbManager):
        cameras (list): list of cameras
        stateless (bool): [optional] if specified use stateless mechanism for camera selection

    Returns:
        Tuple containing camera name, current heading, current timestamp, and filepath of the image
    """
    if getNextImage.tmpDir == None:
        getNextImage.tmpDir = tempfile.TemporaryDirectory()
        logging.warning('TempDir %s', getNextImage.tmpDir.name)

    if getNextImage.queueCamera and (len(getNextImage.queue) > 0):
        camera = getNextImage.queueCamera
    elif stateless:
        camera = cameras[int(len(cameras)*random.random())]
    else:
        counterValue = dbManager.incrementCounter(counterName)
        index = counterValue % len(cameras)
        camera = cameras[index]

    try:
        if len(getNextImage.queue) > 0:
            fetchResult = getNextImage.queue[0]
            getNextImage.queue = getNextImage.queue[1:]
            if len(getNextImage.queue) == 0:
                getNextImage.queueCamera = None
        else:
            fetchResult = img_archive.fetchImageAndMeta(dbManager, camera['name'], camera['url'], getNextImage.tmpDir.name)
        if isinstance(fetchResult, list):
            if len(fetchResult) > 1:
                getNextImage.queue = fetchResult[1:]
                getNextImage.queueCamera = camera
            fetchResult = fetchResult[0]
        (imgPath, heading, timestamp, fov) = fetchResult
        if imgPath == None or heading == None or timestamp == None:
            logging.error('Image or metadata unavailable for %s', camera['name'])
            return (None, None, None, None, None)

        md5 = hashlib.md5(open(imgPath, 'rb').read()).hexdigest()
        if ('md5' in camera) and (camera['md5'] == md5):
            logging.warning('Camera %s image unchanged', camera['name'])
            # skip to next camera
            return (None, None, None, None, None)
        camera['md5'] = md5
    except Exception as e:
        logging.error('Error fetching image from %s %s', camera['name'], str(e))
        return (None, None, None, None, None)

    return (camera['name'], heading, timestamp, fov, imgPath)
getNextImage.tmpDir = None
getNextImage.queue = []
getNextImage.queueCamera = None


# XXXXX Use a fixed stable directory for testing
# from collections import namedtuple
# Tdir = namedtuple('Tdir', ['name'])
# getNextImage.tmpDir = Tdir('c:/tmp/dftest')


def isProto(cameraID, sources=None, protoNum=0):
    if not isProto.prodTypesArr:
        isProto.prodTypesArr = settings.prodTypes.split(',')
    if sources and not isProto.sourcesDict:
        sourcesDict = {}
        for entry in sources:
            sourcesDict[entry['name']] = entry
        isProto.sourcesDict = sourcesDict
    if protoNum and not isProto.protoNum:
        isProto.protoNum = protoNum
    if isProto.protoNum:
        return isProto.protoNum
    type = None
    if isProto.sourcesDict and cameraID in isProto.sourcesDict:
        type = isProto.sourcesDict[cameraID]['type']
    isProd = type and isProto.prodTypesArr and (type in isProto.prodTypesArr)
    # logging.warning('isProd %s: %s, %s, %s', isProd, cameraID, type, isProto.prodTypesArr)
    return not isProd
isProto.sourcesDict = None
isProto.prodTypesArr = None
isProto.protoNum = 0


def drawRect(imgDraw, x0, y0, x1, y1, width, color):
    for i in range(width):
        imgDraw.rectangle((x0 + i, y0 + i, x1 - i, y1 -i), outline=color)


def drawFireBox(img, destPath, fireBoxCoords, timestamp=None, fireSegment=None, color='red', message=''):
    """Draw bounding box with fire detection and optionally write scores

    Also watermarks the image and stores the resulting annotated image as new file

    Args:
        img (Image): Image object to draw on
        destPath (str): filepath where to write the output image
        fireBoxCoords (list): coordinates of fire box (x0, y0, x1, y1)
        fireSegment (dict): [optional] if present, write scores on the image
    """
    imgDraw = ImageDraw.Draw(img)

    (x0, y0, x1, y1) = fireBoxCoords
    lineWidth=2
    drawRect(imgDraw, x0, y0, x1, y1, lineWidth, color)

    fontPath = os.path.join(str(pathlib.Path(os.path.realpath(__file__)).parent.parent), 'firecam/data/Roboto-Regular.ttf')
    if fireSegment:
        # Write ML score above towards left of the fire box
        color = "red"
        fontSize=70
        font = ImageFont.truetype(fontPath, size=fontSize)
        scoreStr = '%.2f' % fireSegment['score']
        textSize = imgDraw.textsize(scoreStr, font=font)
        imgDraw.text((x0, y0 - textSize[1]), scoreStr, font=font, fill=color)

        # Write historical max value above towards right of the fire box
        color = "blue"
        fontSize=60
        font = ImageFont.truetype(fontPath, size=fontSize)
        scoreStr = '%.2f' % fireSegment['HistMax']
        textSize = imgDraw.textsize(scoreStr, font=font)
        imgDraw.text((x1 - textSize[0], y0 - textSize[1]), scoreStr, font=font, fill=color)

    if timestamp:
        fontSize=24
        margin = int(fontSize/2)
        font = ImageFont.truetype(fontPath, size=fontSize)
        timeStr = datetime.datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')
        fullStr = timeStr + ' ' + message
        # first little bit of black outline
        color = "black"
        for i in range(0,5):
            for j in range(0,5):
                imgDraw.text((margin + i, j), fullStr, font=font, fill=color)

        # now actual data in orange
        color = "orange"
        imgDraw.text((margin + 2, 2), fullStr, font=font, fill=color)

    # "watermark" the image
    color = "orange"
    fontSize=20
    font = ImageFont.truetype(fontPath, size=fontSize)
    margin = int(fontSize/2)
    imgDraw.text((margin, img.size[1] - fontSize - margin), "Open Climate Tech - WildfireCheck", font=font, fill=color)

    img.save(destPath, format="JPEG", quality=95)
    del imgDraw


def firePixelCoords(img, fireSegment):
    x0 = fireSegment['MinX'] if 'MinX' in fireSegment else 0
    y0 = fireSegment['MinY'] if 'MinY' in fireSegment else 0
    x1 = fireSegment['MaxX'] if 'MaxX' in fireSegment else img.size[0]
    y1 = fireSegment['MaxY'] if 'MaxY' in fireSegment else img.size[0]
    return (x0, y0, x1, y1)


def genMovie(notificationsDateDir, constants, cameraID, cameraHeading, timestamp, img, imgPath, fireSegment, saveFullImages=True):
    """Generate cropped movie by fetching old images from archive

    Args:
        constants (dict): "global" contants
        cameraID (str): camera name
        timestamp (int): time.time() value when image was taken
        imgPath (str): filepath of the image
        fireSegment (dict): dict describing segment with fire
        cropCoords (list): coordinates for cropping full image
        fileBoxCoords (list): coordinates for highlighting fire box withing cropped region

    Returns:
        Filepath of cropped movie
    """
    (x0, y0, x1, y1) = firePixelCoords(img, fireSegment)
    (cropX0, cropX1) = rect_to_squares.getRangeFromCenter(round((x0 + x1)/2), 640, 0, img.size[0])
    # 412 pixels because that is minimum pixel height in landscape mode for most mobile phones
    (cropY0, cropY1) = rect_to_squares.getRangeFromCenter(round((y0 + y1)/2), 412, 0, img.size[1])
    cropCoords = (cropX0, cropY0, cropX1, cropY1)
    fireBoxCoords = (x0 - cropX0, y0 - cropY0, x1 - cropX0, y1 - cropY0)

    filePathParts = os.path.splitext(imgPath)
    # get images spanning a few minutes so reviewers can evaluate based on progression
    startTimeDT = datetime.datetime.fromtimestamp(timestamp - 4*60) # upto 4 minutes before
    endTimeDT = datetime.datetime.fromtimestamp(timestamp + POST_DETECTION_UPDATE_MINS*60)  # upto POST_DETECTION_UPDATE_MINS minute after
    finalTimestamp = timestamp

    with tempfile.TemporaryDirectory() as tmpDirName:
        imgSequence = img_archive.getArchiveImages(constants['googleServices'], settings, constants['dbManager'], tmpDirName,
                                                    constants['camArchives'], cameraID, cameraHeading, startTimeDT, endTimeDT, 1)
        imgSequence = imgSequence or []
        preImages = []
        detectImage = None
        postImages = []
        for (i, imgFile) in enumerate(imgSequence):
            imgParsed = img_archive.parseFilename(imgFile)
            if imgParsed['unixTime'] < timestamp:
                preImages.append(imgFile)
            elif imgParsed['unixTime'] == timestamp:
                detectImage = imgFile
            else:
                postImages.append(imgFile)
            if (i == len(imgSequence) - 1):
                finalTimestamp = imgParsed['unixTime']
        imgSequence = preImages[-4:] # max 4 most recent images before detection
        imgSequence += [detectImage or imgPath]
        postImages = postImages[:3] # max 3 earliest images after detection
        imgSequence += postImages

        croppedPath = ''
        imgIDs = []
        mspecPath = os.path.join(tmpDirName, 'mspec.txt')
        mspecFile = open(mspecPath, 'w')
        for (i, imgFile) in enumerate(imgSequence):
            imgParsed = img_archive.parseFilename(imgFile)
            if imgParsed['unixTime'] != timestamp:
                algined = img_archive.alignImage(imgFile, imgPath)
                if not algined:
                    continue # skip this image
            if saveFullImages:
                imgIDs.append(goog_helper.copyFile(imgFile, notificationsDateDir))
            cropName = 'img' + ("%03d" % i) + filePathParts[1]
            croppedPath = os.path.join(tmpDirName, cropName)
            imgSeq = Image.open(imgFile)
            croppedImg = imgSeq.crop(cropCoords)
            if imgParsed['unixTime'] < timestamp:
                color = 'yellow'
                message = ''
            elif imgParsed['unixTime'] >= timestamp:
                color = 'red'
                message = 'Potential fire'
            drawFireBox(croppedImg, croppedPath, fireBoxCoords, timestamp=imgParsed['unixTime'], color=color, message=message)
            imgSeq.close()
            croppedImg.close()
            mspecFile.write("file '" + croppedPath + "'\n")
            mspecFile.write('duration 1\n')
        mspecFile.write("file '" + croppedPath + "'\n")
        mspecFile.flush()
        os.fsync(mspecFile.fileno())
        mspecFile.close()
        if saveFullImages and len(imgIDs) < 2: # ignore events without multiple images
            logging.warning('genMovie not enough frames %s, %s, %s, %s', cameraID, len(imgIDs), len(preImages), len(postImages))
            return ('', imgIDs, finalTimestamp, len(postImages))

        # now make movie from this sequence of cropped images
        moviePath = filePathParts[0] + '_' + str(finalTimestamp)[-4:] + '_AnnCrop_' + 'x'.join(list(map(lambda x: str(x), cropCoords))) + '.mp4'
        try:
            (
                ffmpeg.input(mspecPath, format='concat', safe=0)
                    .filter('fps', fps=25, round='up')
                    .output(moviePath, pix_fmt='yuv420p').run()
            )
        except Exception as e:
            logging.error('Error making movie %s', str(e))
        movieID = goog_helper.copyFile(moviePath, notificationsDateDir)
        os.remove(moviePath)

        return (movieID, imgIDs, finalTimestamp, len(postImages))


def genAnnotatedImages(notificationsDateDir, constants, cameraID, cameraHeading, timestamp, imgPath, fireSegment):
    """Generate annotated images (one cropped video, and other full size image)

    Args:
        constants (dict): "global" contants
        cameraID (str): camera name
        timestamp (int): time.time() value when image was taken
        imgPath (str): filepath of the image
        fireSegment (dict): dict describing segment with fire

    Returns:
        Tuple (str, str): filepaths of cropped and full size annotated iamges
    """
    img = Image.open(imgPath)
    (movieID, imgIDs, finalTimestamp, x) = genMovie(notificationsDateDir, constants, cameraID, cameraHeading, timestamp, img, imgPath, fireSegment)
    if not movieID:
        return (movieID, imgIDs, '', finalTimestamp)

    (x0, y0, x1, y1) = firePixelCoords(img, fireSegment)
    filePathParts = os.path.splitext(imgPath)
    annotatedPath = filePathParts[0] + '_Ann' + filePathParts[1]
    drawFireBox(img, annotatedPath, (x0, y0, x1, y1))
    img.close()
    annotatedID = goog_helper.copyFile(annotatedPath, notificationsDateDir)
    os.remove(annotatedPath)

    return (movieID, imgIDs, annotatedID, finalTimestamp)


def drawPolyPixels(mapImg, coordsPixels, fillColor, outlineColor=None):
    """Draw translucent polygon on given map image with given pixel coordinates and fill color

    Args:
        mapImg (Image): existing image
        coordsPixels (list): list of vertices of polygon
        fillColor (list): RGBA values of fill color

    Returns:
        Image object
    """
    mapImgAlpha = mapImg.convert('RGBA')
    polyImg = Image.new('RGBA', mapImgAlpha.size)
    polyDraw = ImageDraw.Draw(polyImg)
    polyDraw.polygon(coordsPixels, fill=fillColor, outline=outlineColor)
    mapImgAlpha.paste(polyImg, mask=polyImg)
    del polyDraw
    polyImg.close()
    return mapImgAlpha.convert('RGB')


def drawPolyLatLong(mapImg, leftLongitude, rightLongitude, topLatitude, bottomLatitude, coords, fillColor, outlineColor=None):
    """Draw translucent polygon on given map image with given lat/long coordinates and fill color

    Args:
        mapImg (Image): existing image
        left/right/top/bottom: borders of map
        coords (list): list of vertices of polygon in lat/long format
        fillColor (list): RGBA values of fill color

    Returns:
        Image object
    """
    coordsPixels = []
    # logging.warning('coords latLong %s', str(coords))
    # first intersect the polygon with map edges to avoid distortions when coverting each point to pixel coordinates
    mapRectangle = [[topLatitude, leftLongitude], [topLatitude, rightLongitude], [bottomLatitude, rightLongitude], [bottomLatitude, leftLongitude]]
    newCoords = getPolygonIntersection(coords, mapRectangle)
    for point in newCoords:
        pixels = img_archive.convertLatLongToPixels(mapImg, leftLongitude, rightLongitude, topLatitude, bottomLatitude, point)
        coordsPixels.append(pixels)
    # logging.warning('coords pixels %s', str(coordsPixels))
    return drawPolyPixels(mapImg, coordsPixels, fillColor, outlineColor=outlineColor)


def getCentroid(polygonCoords):
    poly = Polygon(polygonCoords)
    return list(zip(*poly.centroid.xy))[0]


def cropCentered(mapImg, leftLongitude, rightLongitude, topLatitude, bottomLatitude, polygonCoords):
    """Crop given image to 1/4 size centered at the centroid of given polygon

    Args:
        mapImg (Image): existing image
        left/right/top/bottom: borders of map
        polygonCoords (list): list of vertices of polygon in lat/long format

    Returns:
        Cropped Image object
    """
    centerLatLong = getCentroid(polygonCoords)
    centerXY = img_archive.convertLatLongToPixels(mapImg, leftLongitude, rightLongitude, topLatitude, bottomLatitude, centerLatLong)
    centerX = min(max(centerXY[0], mapImg.size[0]/4), mapImg.size[0]*3/4)
    centerY = min(max(centerXY[1], mapImg.size[1]/4), mapImg.size[1]*3/4)
    coords = (centerX - mapImg.size[0]/4, centerY - mapImg.size[1]/4, centerX + mapImg.size[0]/4, centerY + mapImg.size[1]/4)
    return mapImg.crop(coords)


def getMapSize(mapImgGCS):
    zoomRegex = 'map640z([0-9]+)\.jpg$'
    matches = re.findall(zoomRegex, mapImgGCS)
    if len(matches) != 1:
        return (None, None, None)
    zoom = int(matches[0])
    if (zoom < settings.MAP_ZOOM_MIN) or (zoom > settings.MAP_ZOOM_MAX):
        return (None, None, None)
    # latDiff and longDiff for MAP_ZOOM_MIN
    latDiff = settings.MAP_LAT_DIFF
    longDiff = settings.MAP_LONG_DIFF
    zoomDiff = zoom - settings.MAP_ZOOM_MIN
    if zoomDiff:
        latDiff = latDiff/2**zoomDiff
        longDiff = longDiff/2**zoomDiff
    return (latDiff, longDiff, zoom)


def genAnnotatedMap(mapImgGCS, camLatitude, camLongitude, imgPath, polygon, sourcePolygons, rxBurns):
    """Generate annotated map highlighting potential fire area

    Args:
        mapImgGCS (str): GCS path to map around camera
        camLatitude (float): latitude of camera
        camLongitude (float): longitude of camera
        imgPath (str): filepath of the image
        polygon (list): list of vertices of polygon of potential fire location
        sourcePolygons (list): list of polygons from individual cameras contributing to the polygon

    Returns:
        filepath of annotated map
    """
    # download map from GCS to local
    filePathParts = os.path.splitext(imgPath)
    parsedPath = goog_helper.parseGCSPath(mapImgGCS)
    mapOrig = filePathParts[0] + '_mapOrig.jpg'
    goog_helper.downloadBucketFile(parsedPath['bucket'], parsedPath['name'], mapOrig)

    (mapHeightLat, mapWidthLong, zoom) = getMapSize(mapImgGCS)
    if not mapHeightLat or not mapWidthLong or not zoom:
        return ''
    leftLongitude = camLongitude - mapWidthLong/2
    rightLongitude = camLongitude + mapWidthLong/2
    bottomLatitude = camLatitude - mapHeightLat/2
    topLatitude = camLatitude + mapHeightLat/2

    # markup map to show fire area
    mapImg = Image.open(mapOrig)
    # first draw all source polygons (in light red) that contributed to this fire area
    for (i, sourcePolygon) in enumerate(sourcePolygons):
        lightRed = (255,0,0, 50)
        solidRed = (255,0,0, 255)
        outline = solidRed if i == (len(sourcePolygons) - 1) else None # final polygon is from current detection, and outline it
        mapImg = drawPolyLatLong(mapImg, leftLongitude, rightLongitude, topLatitude, bottomLatitude, sourcePolygon, lightRed, outlineColor=outline)
    # if there were multiple source polygons, highlight the fire area in light blue
    if len(sourcePolygons) > 1:
        lightBlue = (0,0,255, 75)
        mapImg = drawPolyLatLong(mapImg, leftLongitude, rightLongitude, topLatitude, bottomLatitude, polygon, lightBlue)
    # draw any prescribed burns
    for burn in rxBurns:
        if not img_archive.pointInArea(leftLongitude, rightLongitude, topLatitude, bottomLatitude, (burn['latitude'], burn['longitude'])):
            continue
        mapImg = rx_burns.drawRxBurn(mapImg, leftLongitude, rightLongitude, topLatitude, bottomLatitude, (burn['latitude'], burn['longitude']))
    # crop to smaller map centered around fire area
    mapImgCropped = cropCentered(mapImg, leftLongitude, rightLongitude, topLatitude, bottomLatitude, polygon)
    mapCroppedPath = filePathParts[0] + ('_map_z%s.jpg' % zoom)
    mapImgCropped.save(mapCroppedPath, quality=95)
    mapImgCropped.close()
    mapImg.close()
    os.remove(mapOrig)
    return mapCroppedPath


def genAnnotatedMaps(notificationsDateDir, mapFiles, camLatitude, camLongitude, imgPath, polygon, sourcePolygons, rxBurns):
    """Generate annotated map highlighting potential fire area

    Args:
        mapImgGCS (str): GCS path to map around camera
        camLatitude (float): latitude of camera
        camLongitude (float): longitude of camera
        imgPath (str): filepath of the image
        polygon (list): list of vertices of polygon of potential fire location
        sourcePolygons (list): list of polygons from individual cameras contributing to the polygon

    Returns:
        Comma separated URLs for annotated maps
    """
    mapUrls=[]
    for mapImgGCS in mapFiles.split(','):
        mapPath = genAnnotatedMap(mapImgGCS, camLatitude, camLongitude, imgPath, polygon, sourcePolygons, rxBurns)
        if not mapPath:
            continue
        mapID = goog_helper.copyFile(mapPath, notificationsDateDir)
        mapUrl = goog_helper.getUrlForFile(mapID)
        os.remove(mapPath)
        mapUrls.append(mapUrl)
    return ','.join(mapUrls)


def getTriangleVertices(latitude, longitude, heading, rangeAngle):
    """Return list of vertices of the isocelees triangle given lat/long as one vertex
       and heading/rangeAngle specifying the angle to the other vertices.

    Args:
        latitude (float): latitude of central vertex
        longitude (float): longitude of central vertex
        heading (int): direction of the central angle
        rangeAngle (int): degrees (size) of the central angle

    Returns:
        List of all vertices in [lat,long] format
    """
    distanceDegrees = 0.6 # approx 40 miles

    vertices = [[latitude, longitude]]
    angle = 90 - heading
    minAngle = (angle - rangeAngle/2) % 360
    maxAngle = (angle + rangeAngle/2) % 360

    p0Lat = latitude + math.sin(minAngle*math.pi/180)*distanceDegrees
    p0Long = longitude + math.cos(minAngle*math.pi/180)*distanceDegrees
    vertices.append([p0Lat, p0Long])

    p1Lat = latitude + math.sin(maxAngle*math.pi/180)*distanceDegrees
    p1Long = longitude + math.cos(maxAngle*math.pi/180)*distanceDegrees
    vertices.append([p1Lat, p1Long])
    return vertices


def recordProbables(dbManager, cameraID, heading, timestamp, imgPath, fireSegment, modelId, stateless, protoNum):
    """Record that a probable smoke/fire has been observed

    Record the probable detection with useful metrics in 'probables' table in SQL DB.
    Also, upload image file to google cloud

    Args:
        dbManager (DbManager):
        cameraID (str): camera ID
        heading (int): direction camera is facing
        timestamp (int):
        imgPath: filepath of the image
        fireSegment (dictionary): dictionary with information for the segment with fire/smoke

    Returns:
        File IDs for the uploaded image file
    """
    logging.warning('Fire detected by camera %s, image %s, segment %s', cameraID, imgPath, str(fireSegment))
    # copy/upload file to detection dir
    probablesDateDir = goog_helper.dateSubDir(settings.probablesDir)
    fileID = goog_helper.copyFile(imgPath, probablesDateDir)
    logging.warning('Uploaded to probables folder %s', fileID)

    if not stateless:
        dbRow = {
            'CameraName': cameraID,
            'Heading': heading,
            'Timestamp': timestamp,
            'MinX': fireSegment['MinX'],
            'MinY': fireSegment['MinY'],
            'MaxX': fireSegment['MaxX'],
            'MaxY': fireSegment['MaxY'],
            'Score': fireSegment['score'],
            'ImageID': fileID,
            'ModelId': modelId,
            'ProtoNum': protoNum,
            'Hostname': socket.gethostname()
        }
        dbManager.add_data('probables', dbRow)
    return fileID


def isDuplicateProbables(dbManager, cameraID, heading, timestamp, protoNum):
    """Check if this event has already been recently (last hour) discovered for given camera
       This prevents spam from long lasting fires

    Args:
        dbManager (DbManager):
        cameraID (str): camera ID
        heading (int): direction camera is facing
        timestamp (int): time.time() value when image was taken

    Returns:
        True if this is a duplicate probables, False otherwise
    """
    sqlTemplate = """SELECT * FROM probables
    where CameraName='%s' and Heading=%s and timestamp > %s and timestamp < %s and ProtoNum=%s"""
    sqlStr = sqlTemplate % (cameraID, heading, timestamp - 60*60, timestamp, protoNum)

    dbResult = dbManager.query(sqlStr)
    if len(dbResult) > 0:
        logging.warning('Supressing due to recent probables')
        return True
    return False


def getRecentDetections(dbManager, timestamp):
    """Return all recent (last 15 minutes) detections

    Args:
        dbManager (DbManager):
        timestamp (int): time.time() value when image was taken

    Returns:
        List of alerts
    """
    # order by sortId, otherwise a newer timestamp with older sortID (with fewer sourcePolygons) could be ahead
    sqlTemplate = """SELECT * FROM detections where timestamp > %s order by sortid desc"""
    sqlStr = sqlTemplate % (timestamp - 15*60)

    dbResult = dbManager.query(sqlStr)
    return dbResult


def isDuplicateDetection(dbManager, cameraID, fireHeading, rangeAngle, timestamp, protoNum):
    """Check if the location and heading for this event has been recently (last two hours) detected

    Args:
        dbManager (DbManager):
        cameraID (str): camera ID
        heading (int): direction camera is facing
        timestamp (int): time.time() value when image was taken

    Returns:
        True if recently alerted, False otherwise
    """
    sqlTemplate = """SELECT fireheading, angularwidth, isproto FROM detections
                        WHERE timestamp > %s and timestamp < %s and CameraName in (
                            SELECT name FROM sources WHERE locationid = (SELECT locationid FROM sources WHERE name='%s')
                            )"""
    sqlStr = sqlTemplate % (timestamp - 2*60*60, timestamp, cameraID)

    dbResult = dbManager.query(sqlStr)
    if len(dbResult) == 0:
        return False
    for entry in dbResult:
        if protoNum == 0 and int(entry['isproto']) > 1: # for prod model ensure isproto generated from official model (0 or 1)
            continue
        elif protoNum > 0 and protoNum != int(entry['isproto']): # for proto models ensure earlier one matches same proto model
            continue
        if img_archive.intersectsAngleRange(entry['fireheading'], entry['angularwidth'], fireHeading, rangeAngle):
            logging.warning('Supressing due to recent alert')
            return True
    return False


def getPolygonIntersection(coords1, coords2):
    """Find the area intersection of the two given polygons

    Args:
        coords1 (list): vertices of polygon 1
        coords2 (list): vertices of polygon 2

    Returns:
        List of vertices of intersection area or None
    """
    poly1 = Polygon(coords1)
    poly2 = Polygon(coords2)
    if not poly1.intersects(poly2):
        return None
    intPoly = poly1.intersection(poly2)
    if intPoly.area == 0: # point intersections treated as not intersecting
        return None
    # logging.warning('intpoly: %s', str(intPoly))
    intersection = []
    for i in range(len(intPoly.exterior.coords.xy[0])):
        intersection.append([intPoly.exterior.coords.xy[0][i], intPoly.exterior.coords.xy[1][i]])
    return intersection


def intersectRecentDetections(dbManager, timestamp, triangle):
    """Check for area intersection of given triangle with polygons of recent detections

    Args:
        dbManager (DbManager):
        timestamp (int): time.time() value when image was taken
        triangle (list): vertices of triangle

    Returns:
        Intersection area and all source polygons of recent detections
    """
    recentDetections = getRecentDetections(dbManager, timestamp)
    for alert in recentDetections:
        alertCoords = json.loads(alert['polygon'])
        intersection = getPolygonIntersection(triangle, alertCoords)
        if intersection:
            return (intersection, json.loads(alert['sourcepolygons']))


def intersectLand(triangle):
    landVertices = [
        [42.252, -114.000], [42.252, -124.411], #oregon
        [41.996, -124.211], [41.814, -124.231], [41.784, -124.255], [41.746, -124.203], [41.737, -124.159],
        [41.657, -124.134], [41.593, -124.100], [41.562, -124.096], [41.546, -124.075], [41.531, -124.080],
        [41.437, -124.063], [41.286, -124.090], [41.228, -124.086], [41.226, -124.108], [41.157, -124.101],
        [41.156, -124.135], [41.138, -124.157], [41.100, -124.162], [41.070, -124.158], [41.031, -124.116],
        [40.931, -124.131], [40.868, -124.159], [40.844, -124.077], [40.802, -124.135], [40.796, -124.181],
        [40.754, -124.194], [40.723, -124.222], [40.688, -124.201], [40.691, -124.280], [40.443, -124.411],
        [40.242, -124.325], [39.750, -123.825], [39.494, -123.765], [39.362, -123.822], [39.285, -123.798],
        [38.936, -123.723], [38.236, -122.972], [38.026, -123.001], [37.908, -122.649], [37.767, -122.512],
        [37.497, -122.490], [37.327, -122.397], [37.213, -122.419], [36.946, -122.078], [36.946, -121.892],
        [36.788, -121.776], [36.660, -121.826], [36.576, -121.975], [36.546, -121.934], [36.515, -121.947],
        [36.408, -121.918], [36.306, -121.901], [36.237, -121.818], [36.156, -121.671], [36.020, -121.570],
        [36.003, -121.504], [35.881, -121.456], [35.770, -121.325], [35.714, -121.312], [35.671, -121.283],
        [35.642, -121.200], [35.631, -121.159], [35.461, -121.001], [35.445, -120.901], [35.366, -120.866],
        [35.255, -120.897], [35.163, -120.762], [35.164, -120.691], [35.114, -120.635], [35.010, -120.639],
        [34.903, -120.670], [34.884, -120.640], [34.859, -120.608], [34.758, -120.635], [34.705, -120.601],
        [34.568, -120.636], [34.540, -120.549], [34.457, -120.470], [34.464, -120.093], [34.434, -119.953],
        [34.407, -119.860], [34.395, -119.715], [34.420, -119.601], [34.353, -119.434], [34.276, -119.304],
        [34.153, -119.219], [34.084, -119.052], [34.009, -118.808], [34.037, -118.533], [33.824, -118.387],
        [33.773, -118.425], [33.707, -118.289], [33.768, -118.167], [33.617, -117.937], [33.546, -117.801],
        [33.460, -117.714], [33.428, -117.628], [33.378, -117.586], [33.204, -117.390], [33.026, -117.287],
        [32.916, -117.256], [32.849, -117.259], [32.843, -117.286], [32.771, -117.255], [32.664, -117.242],
        [32.592, -117.131], [32.536, -117.124],
        [32.100, -116.950], [32.100, -114.000], # mexico
    ]
    return getPolygonIntersection(triangle, landVertices)


def checkWeatherInfo(weatherModel, dbManager, cameraID, timestamp, fireSegment, polygon, sourcePolygons, cameraLatLong):
    if not weatherModel:
        return 1
    centroidLatLong = getCentroid(polygon)
    (weatherCentroid, weatherCamera) = weather.getWeatherData(dbManager, cameraID, timestamp, centroidLatLong, cameraLatLong)
    if (not weatherCentroid) or (not weatherCamera):
        return 1
    numPolys = len(sourcePolygons)
    imgScore = fireSegment['AdjScore'] if 'AdjScore' in fireSegment else fireSegment['score']
    featureData = weather.normalizeWeather(imgScore, numPolys, weatherCentroid, weatherCamera)
    prediction = weatherModel.predict([featureData])[0][0]
    return prediction


def insertDetectionsDB(dbManager, cameraID, timestamp, croppedUrl, annotatedUrl, mapUrl, fireSegment, polygon, sourcePolygons, imgIDs, sortId, fireHeading, rangeAngle):
    """Add new entry to detections table

    Args:
        dbManager (DbManager):
        cameraID (str): camera name
        timestamp (int): time.time() value when image was taken
        croppedUrl: Public URL for cropped video
        annotatedUrl: Public URL for annotated iamge
        mapUrl: Public URL for annotated map
        fireSegment (dictionary): dictionary with information for the segment with fire/smoke
        polygon (list): list of vertices of polygon of potential fire location
        sourcePolygons (list): list of polygons from individual cameras contributing to the polygon
    """
    dbRow = {
        'CameraName': cameraID,
        'Timestamp': timestamp,
        'AdjScore': fireSegment['AdjScore'] if 'AdjScore' in fireSegment else fireSegment['score'],
        'ImageID': annotatedUrl,
        'CroppedID': croppedUrl,
        'MapID': mapUrl,
        'polygon': str(polygon),
        'sourcePolygons': str(sourcePolygons),
        'IsProto': int(isProto(cameraID)),
        'WeatherScore': fireSegment['weatherScore'],
        'ImgSequence': ','.join(imgIDs),
        'SortId': sortId,
        'FireHeading': fireHeading,
        'AngularWidth': rangeAngle,
    }
    dbManager.add_data('detections', dbRow)


def updateDetectionsDB(dbManager, cameraID, timestamp, croppedUrl, annotatedUrl, mapUrl, imgIDs):
    logging.warning('updateDetectionsDB %s', cameraID)
    sqlTemplate = "SELECT * FROM detections WHERE CameraName='%s' and timestamp = %s"
    sqlStr = sqlTemplate % (cameraID, timestamp)

    dbResult = dbManager.query(sqlStr)
    if len(dbResult) == 0:
        logging.error('updateDetectionsDB: Unexpected no entries')
        return

    sqlTemplate = "UPDATE detections SET CroppedID = '%s', ImageID = '%s', MapID = '%s', ImgSequence = '%s' WHERE CameraName='%s' and timestamp = %s"
    sqlStr = sqlTemplate % (croppedUrl, annotatedUrl, mapUrl, ','.join(imgIDs), cameraID, timestamp)
    dbManager.execute(sqlStr)


def updateDBMovie(dbManager, tableName, cameraID, timestamp, croppedUrl):
    logging.warning('updateDBMovie %s %s', tableName, cameraID)
    sqlTemplate = "SELECT * FROM %s WHERE CameraName='%s' and timestamp = %s"
    sqlStr = sqlTemplate % (tableName, cameraID, timestamp)

    dbResult = dbManager.query(sqlStr)
    if len(dbResult) == 0:
        logging.error('updateDBMovie: Unexpected no entries found in table %s', tableName)
        return

    sqlTemplate = "UPDATE %s SET CroppedID = '%s' WHERE CameraName='%s' and timestamp = %s"
    sqlStr = sqlTemplate % (tableName, croppedUrl, cameraID, timestamp)
    dbManager.execute(sqlStr)


def queryDetections(dbManager, cameraID, timestamp):
    sqlTemplate = "SELECT * FROM detections WHERE CameraName='%s' and timestamp = %s"
    sqlStr = sqlTemplate % (cameraID, timestamp)

    dbResult = dbManager.query(sqlStr)
    if len(dbResult) == 0:
        logging.error('queryDetections: Missing data')
        return None
    detectEntry = dbResult[0]
    return {
        "adjScore": detectEntry['adjscore'],
        'annotatedUrl': detectEntry['imageid'],
        'croppedUrl': detectEntry['croppedid'],
        'mapUrl': detectEntry['mapid'],
        'polygon': json.loads(detectEntry['polygon']),
        'sourcePolygons': json.loads(detectEntry['sourcepolygons']),
        'isProto': detectEntry['isproto'],
        'weatherScore': detectEntry['weatherscore'],
        'sortId': detectEntry['sortid'],
        'fireHeading': detectEntry['fireheading'],
        'angularWidth': detectEntry['angularwidth'],
    }


def insertAlertsDB(dbManager, cameraID, timestamp, croppedUrl, annotatedUrl, mapUrl, fireSegment, polygon, sourcePolygons, sortId, fireHeading, rangeAngle):
    """Add new entry to alerts table

    Args:
        dbManager (DbManager):
        cameraID (str): camera name
        timestamp (int): time.time() value when image was taken
        croppedUrl: Public URL for cropped video
        annotatedUrl: Public URL for annotated iamge
        mapUrl: Public URL for annotated map
        fireSegment (dictionary): dictionary with information for the segment with fire/smoke
        polygon (list): list of vertices of polygon of potential fire location
        sourcePolygons (list): list of polygons from individual cameras contributing to the polygon
    """
    dbRow = {
        'CameraName': cameraID,
        'Timestamp': timestamp,
        'AdjScore': fireSegment['AdjScore'] if 'AdjScore' in fireSegment else fireSegment['score'],
        'ImageID': annotatedUrl,
        'CroppedID': croppedUrl,
        'MapID': mapUrl,
        'polygon': str(polygon),
        'sourcePolygons': str(sourcePolygons),
        'IsProto': int(isProto(cameraID)),
        'WeatherScore': fireSegment['weatherScore'],
        'SortId': sortId,
        'FireHeading': fireHeading,
        'AngularWidth': rangeAngle,
    }
    dbManager.add_data('alerts', dbRow)


def pubsubFireNotification(cameraID, timestamp, croppedUrl, annotatedUrl, mapUrl, fireSegment, polygon, sourcePolygons, sortId, fireHeading):
    """Send a pubsub notification for a potential new fire

    Sends pubsub message with information about the camera and fire score includeing
    image attachments

    Args:
        cameraID (str): camera name
        timestamp (int): time.time() value when image was taken
        croppedUrl: Public URL for cropped video
        annotatedUrl: Public URL for annotated iamge
        mapUrl: Public URL for annotated map
        fireSegment (dictionary): dictionary with information for the segment with fire/smoke
        polygon (list): list of vertices of polygon of potential fire location
    """
    message = {
        'timestamp': timestamp,
        'cameraID': cameraID,
        "adjScore": str(fireSegment['AdjScore'] if 'AdjScore' in fireSegment else fireSegment['score']),
        'annotatedUrl': annotatedUrl,
        'croppedUrl': croppedUrl,
        'mapUrl': mapUrl,
        'polygon': str(polygon),
        'sourcePolygons': str(sourcePolygons),
        'isProto': isProto(cameraID),
        'weatherScore': str(fireSegment['weatherScore']),
        'sortId': sortId,
        'fireHeading': fireHeading,
    }
    goog_helper.publish(message)


def emailFireNotification(constants, cameraID, timestamp, imgPath, annotatedUrl, fireSegment):
    """Send an email alert for a potential new fire

    Send email with information about the camera and fire score includeing
    image attachments

    Args:
        constants (dict): "global" contants
        cameraID (str): camera name
        timestamp (int): time.time() value when image was taken
        imgPath: filepath of the original image
        annotatedUrl: Public URL for annotated iamge
        fireSegment (dictionary): dictionary with information for the segment with fire/smoke
    """
    dbManager = constants['dbManager']
    subject = 'Possible (%d%%) fire in camera %s' % (int(fireSegment['score']*100), cameraID)
    body = 'Please check the attached images for fire.'

    # emails are sent from settings.fuegoEmail and bcc to everyone with active emails in notifications SQL table
    dbResult = dbManager.getNotifications(filterActiveEmail = True)
    emails = [x['email'] for x in dbResult]
    if len(emails) > 0:
        # attach images spanning a few minutes so reviewers can evaluate based on progression
        startTimeDT = datetime.datetime.fromtimestamp(timestamp - 3*60)
        endTimeDT = datetime.datetime.fromtimestamp(timestamp - 1*60)
        with tempfile.TemporaryDirectory() as tmpDirName:
            oldImages = img_archive.getHpwrenImages(constants['googleServices'], settings, tmpDirName,
                                                    constants['camArchives'], cameraID, startTimeDT, endTimeDT, 1)
            attachments = oldImages or []
            attachments.append(imgPath)
            email_helper.sendEmail(constants['googleServices']['mail'], settings.fuegoEmail, emails, subject, body, attachments)


def smsFireNotification(dbManager, cameraID):
    """Send an sms (phone text message) alert for a potential new fire

    Args:
        dbManager (DbManager):
        cameraID (str): camera name
    """
    message = 'Firecam fire notification in camera %s. Please check email for details' % cameraID
    dbResult = dbManager.getNotifications(filterActivePhone = True)
    phones = [x['phone'] for x in dbResult]
    if len(phones) > 0:
        for phone in phones:
            sms_helper.sendSms(settings, phone, message)


def publishAlert(dbManager, cameraID, fireHeading, rangeAngle, timestamp, weatherScore, cameraViewPoly, rxBurns, protoNum):
    if isProto(cameraID):
        return False
    if weatherScore < settings.weatherThreshold:
        return False
    if isDuplicateDetection(dbManager, cameraID, fireHeading, rangeAngle, timestamp, protoNum):
        return False
    # don't publish if rxBurn inside cameraViewPoly
    viewPolygon = Polygon(cameraViewPoly)
    for burn in rxBurns:
        burnPoint = Point(burn['latitude'], burn['longitude'])
        if viewPolygon.intersects(burnPoint):
            return False
    return True


def fireDetected(constants, cameraID, cameraHeading, timestamp, fov, imgPath, fireSegment):
    """Update Detections DB and send alerts about given fire through all channels (pubsub, email, and sms)

    Args:
        constants (dict): "global" contants
        cameraID (str): camera name
        cameraHeading (int): direction camera is facing
        timestamp (int): time.time() value when image was taken
        imgPath: filepath of the original image
        fireSegment (dictionary): dictionary with information for the segment with fire/smoke
    """
    dbManager = constants['dbManager']
    weatherModel = constants['weatherModel']
    protoNum = constants['protoNum']

    # copy annotated image to publicly accessible settings.noticationsDir
    notificationsDateDir = goog_helper.dateSubDir(settings.noticationsDir)
    (mapFiles, camLatitude, camLongitude) = dbManager.getCameraMapLocation(cameraID)

    # get horizontal pixel width
    img = Image.open(imgPath)
    imgSizeX = img.size[0]
    img.close()

    # find angular heading, and check if it should be ignored due to frequent false positives
    (fireHeading, rangeAngle) = img_archive.getHeadingRange(cameraHeading, fov, fireSegment['MinX'], fireSegment['MaxX'], imgSizeX)
    ignoredHeading = img_archive.findIgnoredViewHeading(constants['ignoredViews'], cameraID, fireHeading, rangeAngle)
    if ignoredHeading != None:
        logging.warning('Ignored View %s, %s, %s, %s', cameraID, fireHeading, rangeAngle, ignoredHeading)
        dbManager.incrementIgnoreCounter(cameraID, ignoredHeading)
        return

    triangle = getTriangleVertices(camLatitude, camLongitude, fireHeading, rangeAngle)
    cameraViewPoly = intersectLand(triangle)
    intersectionInfo = intersectRecentDetections(dbManager, timestamp, cameraViewPoly)
    if intersectionInfo:
        polygon = intersectionInfo[0]
        sourcePolygons = intersectionInfo[1] + [cameraViewPoly]
    else:
        polygon = cameraViewPoly
        sourcePolygons = [cameraViewPoly]
    weatherScore = checkWeatherInfo(weatherModel, dbManager, cameraID, timestamp, fireSegment, polygon, sourcePolygons, (camLatitude, camLongitude))
    fireSegment['weatherScore'] = round(weatherScore, 4)

    # sortID makes an impact when the image timestamp order is different than processing time order
    # E.g. (older image processed more recently by multiple seconds)
    # Although processing time is not perfect eigher, it seems slightly better because 1) map intersections will show in increasing order
    # and 2) UI results (and notifications) will be more intuitive
    sortId = int(time.time())
    # insert into detections ASAP so other detections can find the sourcePolygons even before maps and movie are generated and written to DB
    insertDetectionsDB(dbManager, cameraID, timestamp, "", "", "", fireSegment, polygon, sourcePolygons, "", sortId, fireHeading, rangeAngle)

    rxBurns = rx_burns.getCurrentBurns(dbManager)
    mapUrl = genAnnotatedMaps(notificationsDateDir, mapFiles, camLatitude, camLongitude, imgPath, polygon, sourcePolygons, rxBurns)

    (croppedID, imgIDs, annotatedID, finalTimestamp) = genAnnotatedImages(notificationsDateDir, constants, cameraID, cameraHeading, timestamp, imgPath, fireSegment)
    if not croppedID:
        return

    # convert fileIDs into URLs usable by web UI
    croppedUrl = goog_helper.getUrlForFile(croppedID)
    annotatedUrl = goog_helper.getUrlForFile(annotatedID)

    updateDetectionsDB(dbManager, cameraID, timestamp, croppedUrl, annotatedUrl, mapUrl, imgIDs)
    enqueueFireUpdate(constants, cameraID, cameraHeading, timestamp, finalTimestamp, fireSegment)
    if publishAlert(dbManager, cameraID, fireHeading, rangeAngle, timestamp, weatherScore, cameraViewPoly, rxBurns, protoNum):
        insertAlertsDB(dbManager, cameraID, timestamp, croppedUrl, annotatedUrl, mapUrl, fireSegment, polygon, sourcePolygons, sortId, fireHeading, rangeAngle)
        pubsubFireNotification(cameraID, timestamp, croppedUrl, annotatedUrl, mapUrl, fireSegment, polygon, sourcePolygons, sortId, fireHeading)
        emailFireNotification(constants, cameraID, timestamp, imgPath, annotatedUrl, fireSegment)
        smsFireNotification(dbManager, cameraID)


def enqueueFireUpdate(constants, cameraID, cameraHeading, timestamp, finalTimestamp, fireSegment):
    fireUpdateQueue = constants['fireUpdateQueue']
    # assert not already in queue already
    filtered = list(filter(lambda x: (x['cameraID'] == cameraID) and (x['timestamp'] == timestamp), fireUpdateQueue))
    assert len(filtered) == 0
    if time.time() > timestamp + POST_DETECTION_UPDATE_MINS*60: # discard if already POST_DETECTION_UPDATE_MINS minutes post detection time
        logging.warning('enqueueFireUpdate timed out %s', cameraID)
        return
    fireUpdateQueue.append({
        'cameraID': cameraID,
        'cameraHeading': cameraHeading,
        'timestamp': timestamp,
        'finalTimestamp': finalTimestamp,
        'fireSegment': fireSegment,
    })
    # resort by finalTimestamp after append
    constants['fireUpdateQueue'] = sorted(fireUpdateQueue, key=lambda x: x['finalTimestamp'])
    logging.warning('enqueueFireUpdate %s', cameraID)


def popFireUpdate(fireUpdateQueue):
    if len(fireUpdateQueue) > 0:
        if time.time() > fireUpdateQueue[0]['finalTimestamp'] + 60: # one minute after final
            fireEvent = fireUpdateQueue.pop(0)
            return (fireEvent['cameraID'], fireEvent['cameraHeading'], fireEvent['timestamp'], fireEvent['finalTimestamp'], fireEvent['fireSegment'])
    return None


def checkNewImage(constants, cameraID, cameraHeading, timestamp, finalTimestamp):
    logging.warning('checkNewImage %s', cameraID)
    # is there a new image after finalTimestamp?
    startTimeDT = datetime.datetime.fromtimestamp(finalTimestamp + 31) # at least half a minute after most recent image
    endTimeDT = datetime.datetime.fromtimestamp(timestamp + POST_DETECTION_UPDATE_MINS*60)
    newImages = False
    with tempfile.TemporaryDirectory() as tmpDirName:
        images = img_archive.getArchiveImages(constants['googleServices'], settings, constants['dbManager'], tmpDirName,
                                                 constants['camArchives'], cameraID, cameraHeading, startTimeDT, endTimeDT, 1)
        if len(images) == 0:
            return False
        lastImage = images[-1]
        imgParsed = img_archive.parseFilename(lastImage)
        if imgParsed['unixTime'] > finalTimestamp:
            newImages = True
    return newImages


def updateMovie(constants, cameraID, cameraHeading, timestamp, fireSegment):
    logging.warning('updateMovie %s', cameraID)
    # need local image from detection time for alignment and sizing coordinates from archive
    timeDT = datetime.datetime.fromtimestamp(timestamp)
    isFinalMovie = False
    with tempfile.TemporaryDirectory() as tmpDirName:
        imgPath = img_archive.getArchiveImages(constants['googleServices'], settings, constants['dbManager'], tmpDirName,
                                                constants['camArchives'], cameraID, cameraHeading, timeDT, timeDT, 1)
        if not imgPath:
            return (None, None)
        imgPath = imgPath[-1]
        img = Image.open(imgPath)
        notificationsDateDir = goog_helper.dateSubDir(settings.noticationsDir)
        (movieID, imgIDs, finalTimestamp, postCount) = genMovie(notificationsDateDir, constants, cameraID, cameraHeading, timestamp, img, imgPath, fireSegment, saveFullImages=False)
        isFinalMovie = postCount > 2
        img.close()
    return (movieID, finalTimestamp, isFinalMovie)


def processEnqueuedUpdates(constants):
    fireUpdateQueue = constants['fireUpdateQueue']
    dbManager = constants['dbManager']
    protoNum = constants['protoNum']
    fireEvent = popFireUpdate(fireUpdateQueue)
    if not fireEvent:
        return
    (cameraID, cameraHeading, timestamp, finalTimestamp, fireSegment) = fireEvent
    logging.warning('processEnqueuedUpdates %s', cameraID)
    reQueue = True
    detectData = queryDetections(dbManager, cameraID, timestamp)
    if detectData and checkNewImage(constants, cameraID, cameraHeading, timestamp, finalTimestamp):
        # XXXXX TODO: score new images for smoke
        (movieID, finalTimestamp, isFinalMovie) = updateMovie(constants, cameraID, cameraHeading, timestamp, fireSegment)
        reQueue = not isFinalMovie
        if movieID and finalTimestamp:
            movieUrl = goog_helper.getUrlForFile(movieID)
            movieUrls = movieUrl + ',' + detectData['croppedUrl']
            updateDBMovie(dbManager, 'detections', cameraID, timestamp, movieUrls)
            sourcePolygons = detectData['sourcePolygons']
            cameraViewPoly = sourcePolygons[-1]
            rxBurns = rx_burns.getCurrentBurns(dbManager)
            if publishAlert(dbManager, cameraID, detectData['fireHeading'], detectData['angularWidth'], timestamp, detectData['weatherScore'], cameraViewPoly, rxBurns, protoNum):
                updateDBMovie(dbManager, 'alerts', cameraID, timestamp, movieUrls)
                pubsubFireNotification(cameraID, timestamp, movieUrls, detectData['annotatedUrl'], detectData['mapUrl'], fireSegment, detectData['polygon'], detectData['sourcePolygons'], detectData['sortId'], detectData['fireHeading'])
        else:
            logging.error('processEnqueuedUpdates: failure %s: %s, %s', cameraID, movieID, finalTimestamp)
            return # don't requeue
    # XXXX TODO: should timestamp change for requeue depending of result of checkNewImage
    if reQueue:
        enqueueFireUpdate(constants, cameraID, cameraHeading, timestamp, finalTimestamp, fireSegment)
    else:
        logging.warning('processEnqueuedUpdates finalMovie %s', cameraID)


def deleteImageFiles(imgPath, origImgPath):
    """Delete all image files given in segments

    Args:
        imgPath: filepath of the processed image
        origImgPath: filepath of the original image
    """
    os.remove(imgPath)
    if imgPath != origImgPath:
        os.remove(origImgPath)
    # ppath = pathlib.PurePath(imgPath)
    # leftoverFiles = os.listdir(str(ppath.parent))
    # if len(leftoverFiles) > 0:
    #     logging.warning('leftover files %s', str(leftoverFiles))


def heartBeat(filename):
    """Inform monitor process that this detection process is alive

    Informs by updating the timestamp on given file

    Args:
        filename (str): file path of file used for heartbeating
    """
    pathlib.Path(filename).touch()


def updateTimeTracker(timeTracker, processingTime):
    """Update the time tracker data with given time to process current image

    If enough samples new samples have been reorded, resets the history and
    updates the average timePerSample

    Args:
        timeTracker (dict): tracks recent image processing times
        processingTime (float): number of seconds needed to process current image
    """
    timeTracker['totalTime'] += processingTime
    timeTracker['numSamples'] += 1
    # after N samples, update the rate to adapt to current conditions
    # N = 50 should be big enough to be stable yet small enough to adapt
    if timeTracker['numSamples'] > 50:
        timeTracker['timePerSample'] = timeTracker['totalTime'] / timeTracker['numSamples']
        timeTracker['totalTime'] = 0
        timeTracker['numSamples'] = 0
        logging.warning('New timePerSample %.2f', timeTracker['timePerSample'])


def initializeTimeTracker():
    """Initialize the time tracker

    Returns:
        timeTracker (dict):
    """
    return {
        'totalTime': 0.0,
        'numSamples': 0,
        'timePerSample': 3 # start off with estimate of 3 seconds per camera
    }


def getArchivedImages(constants, cameras, startTimeDT, timeRangeSeconds):
    """Get random images from HPWREN archive matching given constraints and optionally subtract them

    Args:
        constants (dict): "global" contants
        cameras (list): list of cameras
        startTimeDT (datetime): starting time of time range
        timeRangeSeconds (int): number of seconds in time range

    Returns:
        Tuple containing camera name, current timestamp, filepath of regular image, and filepath of difference image
    """
    if getArchivedImages.tmpDir == None:
        getArchivedImages.tmpDir = tempfile.TemporaryDirectory()
        logging.warning('TempDir %s', getArchivedImages.tmpDir.name)

    # setup caching of the archive files locally
    if (getArchivedImages.cache == None) and settings.downloadDir:
        writable = os.access(settings.downloadDir, os.W_OK|os.X_OK)
        writeDirPath = settings.downloadDir
        if not writable:
            getArchivedImages.writeDir = tempfile.TemporaryDirectory()
            writeDirPath = getArchivedImages.writeDir.name
        getArchivedImages.cache = img_archive.cacheDir(settings.downloadDir, writeDirPath)

    if getArchivedImages.cache:
        downloadDirOrCache = getArchivedImages.cache
    else:
        downloadDirOrCache = getArchivedImages.tmpDir.name

    cameraID = cameras[int(len(cameras)*random.random())]['name']
    timeDT = startTimeDT + datetime.timedelta(seconds = random.random()*timeRangeSeconds)
    # ensure time between 8AM and 8PM because currently focusing on daytime only
    if timeDT.hour < 8:
        timeDT += datetime.timedelta(hours=8)
    elif timeDT.hour >= 20:
        timeDT -= datetime.timedelta(hours=4)
    files = img_archive.getHpwrenImages(constants['googleServices'], settings, downloadDirOrCache,
                                        constants['camArchives'], cameraID, timeDT, timeDT, 1)
    # logging.warning('files %s', str(files))
    if not files:
        return (None, None, None, None)

    # if in cache mode, copy files to temporary directory because they will be deleted later by main loop
    if getArchivedImages.cache:
        tmpFiles = []
        for srcFilePath in files:
            srcFilePP = pathlib.PurePath(srcFilePath)
            destPath = os.path.join(getArchivedImages.tmpDir.name, str(srcFilePP.name))
            shutil.copy(srcFilePath, destPath)
            tmpFiles.append(destPath)
        files = tmpFiles

    if len(files) > 0:
        parsedName = img_archive.parseFilename(files[0])
        return (cameraID, parsedName['unixTime'], files[0], files[0])
    return (None, None, None, None)
getArchivedImages.tmpDir = None
getArchivedImages.cache = None


def fetchPriorAligned(constants, cameraID, heading, timestamp, baseImgPath, outputDirName):
    imgDT = datetime.datetime.fromtimestamp(timestamp)
    # target 1 minute (60 seconds) prior by setting range from 1.5 to 0.5 minutes prior
    startDT = imgDT - datetime.timedelta(seconds = 90)
    endDT = imgDT - datetime.timedelta(seconds = 31)
    oldImages = img_archive.getArchiveImages(constants['googleServices'], settings, constants['dbManager'], outputDirName,
                    constants['camArchives'], cameraID, heading, startDT, endDT, 1)
    if not oldImages:
        return None
    priorImg = None
    if len(oldImages) >= 1:
        # find the most recent aligned image
        oldImages.reverse()
        for filePath in oldImages:
            imgParsed = img_archive.parseFilename(filePath)
            if imgParsed['unixTime'] == timestamp:  # skip current image if somehow that sneaks in
                continue
            if img_archive.isPTZ(cameraID): # PTZ iamges require alignment
                img = img_archive.alignImageObj(filePath, baseImgPath)
                if img:
                    priorImg = img
                    break
            else:
                priorImg = Image.open(oldImages[0])
                break
    if priorImg:
        priorImg.load() # force load to allow remove below to succeed on Windows
    for filePath in oldImages:
        os.remove(filePath)
    return priorImg


def fetchDiffImage(constants, cameraID, heading, timestamp, baseImgPath, outputDirName):
    priorImg = fetchPriorAligned(constants, cameraID, heading, timestamp, baseImgPath, outputDirName)
    if not priorImg:
        return None
    imgOrig = Image.open(baseImgPath)
    return img_archive.diffWithChecks(imgOrig, priorImg)


def getGroupConfig(detectGroup):
    if detectGroup:
        groupName = detectGroup
    else:
        groupName = goog_helper.getInstanceGroup()
    if not groupName:
        return None
    groupName = groupName.split('/').pop() # get last path component
    if not settings.detectGroups:
        return None
    groupConfig = next(filter(lambda x: x[0] == groupName, settings.detectGroups), None)
    assert groupConfig and len(groupConfig) > 0
    groupParams = {
        'name': groupConfig[0],
        'numInstances': groupConfig[1],
        'counterName': groupConfig[2],
        'restrictType': groupConfig[3],
    }
    if len(groupConfig) > 4:
        protoModelInfo = groupConfig[4]
        protoModelParts = protoModelInfo.split(';')
        groupParams['protoNum'] = protoModelParts[0]
        groupParams['protoPolicy'] = protoModelParts[1]
        groupParams['protoPolicyParams'] = protoModelParts[2]
        groupParams['useWeatherModel'] = protoModelParts[3] == '1'
    logging.warning('GroupConfig %s', groupParams)
    return groupParams


def main():
    optArgs = [
        ["b", "heartbeat", "filename used for heartbeating check"],
        ["c", "collectPositves", "collect positive segments for training data"],
        ["t", "time", "Time breakdown for processing images"],
        ["r", "restrictType", "Only process images from cameras of given type"],
        ["d", "counterName", "Name of row in counters table"],
        ["n", "noState", "(optional) no changes to state"],
        ["s", "startTime", "(optional) performs search with modifiedTime > startTime"],
        ["e", "endTime", "(optional) performs search with modifiedTime < endTime"],
        ["z", "randomSeed", "(optional) override random seed"],
        ["o", "randomOffset", "(optional) random offset - skip given number of random images", int],
        ["l", "limitImages", "(optional) stop after processing given number of images", int],
        ["g", "detectGroup", "(optional) detectGroup to use vs. checking GCP instance group"],
    ]
    args = collect_args.collectArgs([], optionalArgs=optArgs, parentParsers=[goog_helper.getParentParser()])
    limitImages = args.limitImages if args.limitImages else 1e9
    # TODO: Fix googleServices auth to resurrect email alerts
    # googleServices = goog_helper.getGoogleServices(settings, args)
    googleServices = None
    dbManager = db_manager.DbManager(sqliteFile=settings.db_file,
                                    psqlHost=settings.psqlHost, psqlDb=settings.psqlDb,
                                    psqlUser=settings.psqlUser, psqlPasswd=settings.psqlPasswd)
    groupConfig = getGroupConfig(args.detectGroup)
    if args.restrictType:
        restrictType = args.restrictType
    elif groupConfig:
        restrictType = groupConfig['restrictType']
    else:
        restrictType = None
    logging.warning('RestrictType %s', restrictType)

    cameras = dbManager.get_sources(activeOnly=True, restrictType=restrictType)
    logging.warning('Found %d cameras', len(cameras))
    if len(cameras) == 0:
        return
    protoNum = groupConfig['protoNum'] if (groupConfig and 'protoNum' in groupConfig) else 0
    isProto(None, sources=cameras, protoNum=protoNum)
    usableRegions = dbManager.get_usable_regions_dict()
    ignoredViews = dbManager.get_ignoredViews()

    if args.counterName:
        counterName = args.counterName
    elif groupConfig:
        counterName = groupConfig['counterName']
    else:
        counterName = 'sources'
    logging.warning('Counter name %s', counterName)

    startTimeDT = dateutil.parser.parse(args.startTime) if args.startTime else None
    endTimeDT = dateutil.parser.parse(args.endTime) if args.endTime else None
    timeRangeSeconds = None
    useArchivedImages = False
    stateless = True if args.noState else False
    if startTimeDT or endTimeDT:
        assert startTimeDT and endTimeDT
        timeRangeSeconds = (endTimeDT-startTimeDT).total_seconds()
        assert timeRangeSeconds > 0
        assert args.collectPositves
        useArchivedImages = True
        stateless = True
        # if seed not specified, use os.urandom and log value
        randomSeed = args.randomSeed if args.randomSeed else os.urandom(4).hex()
        logging.warning('Random seed %s', randomSeed)
        random.seed(randomSeed, version=2)
        if args.randomOffset:
            for x in range(args.randomOffset):
                # use two random()s each iteration to match getArchivedImages
                random.random()
                random.random()
    camArchives = img_archive.getHpwrenCameraArchives(settings.hpwrenArchives)
    if groupConfig and 'protoPolicy' in groupConfig:
        DetectionPolicyClass = policies.get_policies()[groupConfig['protoPolicy']]
        protoPolicyParams = getattr(settings, groupConfig['protoPolicyParams'])
        detectionPolicy = DetectionPolicyClass(args, dbManager, stateless=stateless, modelLocation=protoPolicyParams)
    else:
        DetectionPolicyClass = policies.get_policies()[settings.detectionPolicy]
        detectionPolicy = DetectionPolicyClass(args, dbManager, stateless=stateless)
    logging.warning('weatherModel %s threshold %s', settings.weather_model, settings.weatherThreshold)
    weatherModel = tf_helper.loadModel(settings.weather_model)
    fireUpdateQueue = []
    constants = { # dictionary of constants to reduce parameters in various functions
        'args': args,
        'googleServices': googleServices,
        'camArchives': camArchives,
        'dbManager': dbManager,
        'weatherModel': weatherModel,
        'ignoredViews': ignoredViews,
        'fireUpdateQueue': fireUpdateQueue,
        'protoNum': protoNum,
    }
    if protoNum and not groupConfig['useWeatherModel']:
        constants['weatherModel'] = None
        logging.warning('Clearning weatherModel for proto')

    numImages = 0
    numProbables = 0
    numAlerts = 0
    processingTimeTracker = initializeTimeTracker()
    while True:
        processEnqueuedUpdates(constants)
        classifyImgPath = None
        timeStart = time.time()
        if useArchivedImages:
            (cameraID, timestamp, imgPath, classifyImgPath) = \
                getArchivedImages(constants, cameras, startTimeDT, timeRangeSeconds)
            if cameraID:
                heading = img_archive.getHeading(cameraID)
            fov = img_archive.getCameraFov(cameraID)
        else: # regular (non diff mode), grab image and process
            (cameraID, heading, timestamp, fov, imgPath) = getNextImage(dbManager, cameras, stateless, counterName)
            classifyImgPath = imgPath
        if not cameraID:
            continue # skip to next camera
        timeFetch = time.time()

        image_spec = [{}]
        image_spec[-1]['path'] = classifyImgPath
        image_spec[-1]['timestamp'] = timestamp
        image_spec[-1]['cameraID'] = cameraID
        image_spec[-1]['heading'] = heading
        if cameraID in usableRegions:
            usableEntry = usableRegions[cameraID]
            if 'startY' in usableEntry:
                image_spec[-1]['startY'] = usableEntry['startY']
            if 'endY' in usableEntry:
                image_spec[-1]['endY'] = usableEntry['endY']
        # ignore top and bottom 50 (cloud, metadata, too nearby)
        if ('startY' not in image_spec[-1]) or not image_spec[-1]['startY']:
            image_spec[-1]['startY'] = 50
        if ('endY' not in image_spec[-1]) or not image_spec[-1]['endY']:
            image_spec[-1]['endY'] = -50

        detectionResult = detectionPolicy.detect(image_spec, checkShifts=True,
                            fetchDiff=lambda x: fetchDiffImage(constants, cameraID, heading, timestamp, classifyImgPath, x))
        timeDetect = time.time()
        numImages += 1
        fireSegment = detectionResult['fireSegment']
        if fireSegment:
            numProbables += 1
        if fireSegment and not useArchivedImages:
            recordProbables(dbManager, cameraID, heading, timestamp, imgPath, fireSegment, detectionPolicy.modelId, stateless, protoNum)
            if not (isDuplicateProbables(dbManager, cameraID, heading, timestamp, protoNum) or stateless):
                fireDetected(constants, cameraID, heading, timestamp, fov, imgPath, fireSegment)
                numAlerts += 1
        if not stateless and not protoNum:
            img_archive.markImageProcessed(dbManager, cameraID, heading, timestamp)
        deleteImageFiles(classifyImgPath, imgPath)
        if (args.heartbeat):
            heartBeat(args.heartbeat)

        timePost = time.time()
        updateTimeTracker(processingTimeTracker, timePost - timeStart)
        if args.time:
            if not detectionResult['timeMid']:
                detectionResult['timeMid'] = timeDetect
            logging.warning('Timings: fetch=%.2f, detect0=%.2f, detect1=%.2f post=%.2f',
                timeFetch-timeStart, detectionResult['timeMid']-timeFetch, timeDetect-detectionResult['timeMid'], timePost-timeDetect)
        if (numImages % 10) == 0:
            logging.warning('Stats: alerts=%d, detects=%d, images=%d', numAlerts, numProbables, numImages)
            if numImages >= limitImages:
                logging.warning('Reached limit on images')
                return
        # free all memory for current iteration and trigger GC to prevent memory growth
        detectionResult = None
        gc.collect()

if __name__=="__main__":
    main()
