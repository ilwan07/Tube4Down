import pytubefix as pytube
import PyQt5.QtWidgets as Qt
from PyQt5 import QtCore
from PyQt5 import QtGui
import PyQt5.QtWebEngineWidgets as QtWeb
from PyQt5.QtCore import QThread, pyqtSignal
from PIL import Image
from bs4 import BeautifulSoup
from logging.handlers import RotatingFileHandler
import logging as log
import threading as thr
import requests
import urllib
import urllib.error
import os
import sys
import shutil
import glob
import time
import io
import zipfile
import tarfile
import subprocess


def clean_subprocess_env():
    """environment to use for any spawned subprocess"""
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        lp_orig = env.get("LD_LIBRARY_PATH_ORIG")
        if lp_orig is not None:
            env["LD_LIBRARY_PATH"] = lp_orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env

# detect dark mode
if sys.platform == "win32":  # on windows, use darkdetect
    import darkdetect
    IS_DARK = darkdetect.isDark()
else:  # use xdg desktop portal
    import re
    IS_DARK = False
    try:
        result = subprocess.run(
            ["gdbus", "call", "--session",
                "--dest", "org.freedesktop.portal.Desktop",
                "--object-path", "/org/freedesktop/portal/desktop",
                "--method", "org.freedesktop.portal.Settings.Read",
                "org.freedesktop.appearance", "color-scheme"],
            capture_output=True, text=True, timeout=2,
            env=clean_subprocess_env(),
        )
        match = re.search(r"uint32 (\d)", result.stdout)
        if match:
            IS_DARK = True if int(match.group(1)) == 1 else False
    except Exception:
        pass


# get script location for assets
if getattr(sys, "frozen", False):  # running as a pyinstaller-built exe
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def asset(relative_path):
    """Resolve a path inside the assets folder regardless of the current working directory"""
    if IS_DARK:
        return os.path.join(SCRIPT_DIR, "assets", "dark", relative_path)
    else:
        return os.path.join(SCRIPT_DIR, "assets", "light", relative_path)

# location for dynamic data
if sys.platform == "win32":
    # running as an app installed with a setup (has a .setup empty file in the exe folder)
    if os.path.exists(os.path.join(SCRIPT_DIR, ".setup")):
        DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local")), "Tube4Down")
    else:  # running as a portable app
        DATA_DIR = SCRIPT_DIR
else:
    DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "Tube4Down")

os.makedirs(DATA_DIR, exist_ok=True)
os.chdir(DATA_DIR)

# properly locate system CA certificate
if getattr(sys, "frozen", False):
    cert_path = os.path.join(getattr(sys, "_MEIPASS", SCRIPT_DIR), "certifi", "cacert.pem")
    if os.path.isfile(cert_path):
        os.environ["SSL_CERT_FILE"] = cert_path
        os.environ["REQUESTS_CA_BUNDLE"] = cert_path

handler = RotatingFileHandler("latest.log", maxBytes=1048576, backupCount=0)  # 1MiB limit
handler.setFormatter(log.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
log.basicConfig(level=log.DEBUG, handlers=[handler])  # configure logging


class YTDownloader(Qt.QMainWindow):
    """YouTube video downloader with GUI"""

    class DownloadWindow(Qt.QWidget):
        """Window displaying the download progress"""
        
        def __init__(self, to_download:dict, type:str, settings:dict):
            self.to_download = to_download  # dictionary of video ids to download in the form {video_id: file_name}
            self.video_ids = []  # list of video ids
            self.video_file_names = []  # list of file names
            for key, value in self.to_download.items():
                self.video_ids.append(key)
                self.video_file_names.append(value)
            self.type = type  # type of media to download (video or audio)
            self.settings = settings  # settings for the download
            self.video_index = -1  # current video index starting at 0 (-1 for initialization)
            super().__init__()  # initialize the window
            self.setWindowTitle("Téléchargement")  # set the window title
            self.setWindowIcon(QtGui.QIcon(asset("icon.ico")))  # set the window icon
            self.setWindowModality(QtCore.Qt.ApplicationModal)  # prevent interaction while downloading
            self.build_ui()
            log.debug("Init download progress window done")
            self.show()

        def build_ui(self):
            """build the UI and elements"""
            self.main_layout = Qt.QVBoxLayout()
            self.setLayout(self.main_layout)

            self.download_label = Qt.QLabel("Initializing download...")
            self.download_label.setFont(QtGui.QFont("Arial", 20))
            self.download_label.setAlignment(QtCore.Qt.AlignCenter)
            self.main_layout.addWidget(self.download_label)

            self.progress_bar = Qt.QProgressBar()
            self.progress_bar.setValue(0)
            self.progress_bar.setFont(QtGui.QFont("Arial", 16))
            self.main_layout.addWidget(self.progress_bar)

            self.bottom_info_widget = Qt.QWidget()
            self.bottom_info_layout = Qt.QHBoxLayout()
            self.bottom_info_widget.setLayout(self.bottom_info_layout)
            self.bottom_info_widget.setFont(QtGui.QFont("Arial", 16))
            self.main_layout.addWidget(self.bottom_info_widget)

            self.file_number_label = Qt.QLabel(str(self.video_index+1))
            self.file_number_total_label = Qt.QLabel(f"/ {len(self.to_download)}")
            self.bottom_info_layout.addWidget(self.file_number_label)
            self.bottom_info_layout.addWidget(self.file_number_total_label)

            self.bottom_info_layout.addStretch()

            self.bytes_progress_label = Qt.QLabel("0 B")
            self.bytes_total_label = Qt.QLabel("/ 0 B")
            self.bottom_info_layout.addWidget(self.bytes_progress_label)
            self.bottom_info_layout.addWidget(self.bytes_total_label)
            self.bottom_info_widget.setFont(QtGui.QFont("Arial", 16))
        
        def download(self):
            """download one video at a time and call itself for each video in the list"""
            self.video_index += 1
            if self.video_index >= len(self.to_download):
                log.info("Finished downloading videos")
                Qt.QMessageBox.information(self, "Done", "Downloaded all videos successfully.")
                self.close()
                return
            log.info(f"Initializing download for video: {self.video_ids[self.video_index]}")
            video_id = self.video_ids[self.video_index]
            file_name = self.video_file_names[self.video_index]
            self.file_number_label.setText(str(self.video_index+1))
            self.settings["file_name"] = file_name
            self.video = YTDownloader.Downloader(video_id, self.type, self.settings)
            self.video.get_best_streams()
            self.file_size = self.video.total_size
            self.bytes_total_label.setText(f"/ {YTDownloader.standard_size(self, self.file_size)}")
            self.download_label.setText(f"Downloading '{file_name}'...")
            self.progress_bar.setMaximum(100)
            self.video.progress.connect(self.update_infos)
            self.video.converting.connect(self.converting)
            self.video.finished.connect(self.download)
            self.video.start()

        def update_infos(self, downloaded:int):
            """update the download information such as the progress and the downloaded size in bytes"""
            self.progress_bar.setValue(round(((downloaded)/self.file_size)*100))
            self.bytes_progress_label.setText(YTDownloader.standard_size(self, downloaded))

        def converting(self):
            """update the download information when the file is being converted"""
            self.download_label.setText(f"Processing '{self.video_file_names[self.video_index]}'...")
        
        def closeEvent(self, event):
            """handle closing the download window"""
            # just close if download is done
            if self.video_index >= len(self.to_download):
                event.accept()
                return
            reply = Qt.QMessageBox.question(self, "Cancel", "Do you want to cancel the download?",
                                             Qt.QMessageBox.Yes | Qt.QMessageBox.No, Qt.QMessageBox.No)
            if reply == Qt.QMessageBox.Yes:
                # stop all downloads, clean the media cache and close the window
                if hasattr(self, 'video') and self.video.isRunning():
                    self.video.terminate()
                time.sleep(0.2)
                for file in glob.glob("cache/media/*") + glob.glob("cache/videos/*") + glob.glob("cache/audios/*"):
                    try:
                        os.remove(file)
                    except Exception as e:
                        log.warning(f"Can't delete cached file {file} after cancellation: {e}")
            else:
                event.ignore()
    

    class Downloader(QThread):
        """Class containing all the needed functions to download a video or audio with the desired settings"""
        progress = pyqtSignal("qint64")  # signal to send the download progress in remaining bytes (64 bits to handle large sizes)
        converting = pyqtSignal()  # signal to send when the file is being converted
        finished = pyqtSignal()  # signal to send when the download is finished

        def __init__(self, video_id:str, media_type:str, settings:dict):
            """the type can be "video" or "audio" and the settings are the video quality, if there is audio, the format, the file name and the save path, use measure_size to get the total file size and not download anything"""
            super().__init__()
            self.video_id = video_id
            self.type = media_type
            self.settings = settings
            self.total_size = 0  # total file size in bytes
            self.downloaded_bytes = 0  # downloaded bytes so far
            self.found_stream = False  # is the correct stream found yet

            self.qualities = ["2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p"]
            self.video_url = f"https://www.youtube.com/watch?v={self.video_id}"

            if self.type == "video":
                self.quality = self.settings["quality"].split(" ")[0]
                if self.quality.lower() == "max":
                    self.quality = self.qualities[0]
                self.has_audio = self.settings["has_audio"]
            else:
                self.quality = None
                self.has_audio = None
            self.format = self.settings["format"].lower()
            # Sanitize file name by removing illegal Windows characters
            illegal_chars = r'\/:*?"<>|'
            sanitized_name = self.settings["file_name"]
            for char in illegal_chars:
                sanitized_name = sanitized_name.replace(char, "")
            self.file_name = sanitized_name
            self.save_path = self.settings["save_path"]
            self.video = pytube.YouTube(self.video_url, on_progress_callback=self.emit_progress)  # video object
            log.debug(f"Init downloader object for video {self.video_id}")

        def run(self):
            """start the download process"""
            self.download()
        
        def emit_progress(self, stream, data_block, remaining_bytes):
            """emits the total downloaded bytes"""
            self.downloaded_bytes += len(data_block)
            self.progress.emit(self.downloaded_bytes)
        
        def download(self):
            """Download the video or audio with the desired settings"""
            log.info(f"About to start download for video {self.video_id}")
            if not self.found_stream:
                self.get_best_streams()
            self.download_base_files()
            self.convert_file()
        
        def get_best_streams(self):
            """determine the best data streams depending on the quality and format settings"""
            # finding the best stream for the used settings and choosing the required resolution quality
            if self.type == "video":
                self.video_instances = self.video.streams.filter(adaptive=True, type="video")
                quality_index = self.qualities.index(self.quality)
                self.quality_ranked = [self.qualities[quality_index]] + self.qualities[quality_index+1:] + self.qualities[:quality_index][::-1]  # ordered list of qualities, preffering the first available one
                for quality in self.quality_ranked:
                    self.video_instances_quality = self.video_instances.filter(res=quality)
                    if self.video_instances_quality:
                        self.used_quality = quality
                        break
                # choosing the best refresh rate, if possible with the wanted format
                self.video_instances_quality = self.video_instances_quality.order_by("fps").desc()
                best_fps = self.video_instances_quality.first().fps
                self.video_instances_quality = self.video_instances_quality.filter(fps=best_fps)
                if self.video_instances_quality.filter(mime_type=f"video/{self.format}"):
                    self.video_instance = self.video_instances_quality.filter(mime_type=f"video/{self.format}").first()
                else:
                    self.video_instance = self.video_instances_quality.first()
                # getting the file type
                self.video_instance_file_type = self.video_instance.mime_type.split("/")[1]
                self.total_size += self.video_instance.filesize
                log.debug(f"Got best stream for video {self.video_id}: {self.used_quality}")
            
            if self.type == "audio" or self.has_audio:
                # choosing the best audio quality
                self.audio_instances = self.video.streams.filter(adaptive=True, type="audio").order_by("abr")
                self.audio_instance = self.audio_instances.last()
                self.audio_instance_file_type = self.audio_instance.mime_type.split("/")[1]
                self.total_size += self.audio_instance.filesize
                log.debug(f"Got best stream for audio {self.video_id}")
            self.found_stream = True
        
        def download_base_files(self):
            """Download the files (video and/or audio) with the default format in the cache (webp/mp4 for both video and audio)"""
            log.info(f"Downloading base file for video {self.video_id}")
            if self.type == "video":
                self.video_instance.download("cache/videos", filename=f"{self.video_id}.{self.video_instance_file_type}")
            if self.type == "audio" or self.has_audio:
                self.audio_instance.download("cache/audios", filename=f"{self.video_id}.{self.audio_instance_file_type}")

        def convert_file(self):
            """If needed, convert the file to the desired format, merge audio and video, and move it to the save path"""
            log.info(f"Starting conversion for video {self.video_id}")
            self.converting.emit()  # send the converting signal to the main thread
            if sys.platform == "win32":  # windows
                ffmpeg_path = "ffmpeg\\bin\\ffmpeg.exe"
            else:
                ffmpeg_path = "ffmpeg/ffmpeg"
            log.debug(f"Using ffmpeg path {os.path.abspath(ffmpeg_path)}")
            if not os.path.exists(ffmpeg_path):
                log.critical("ffmpeg binary not found, can't process")
                raise RuntimeError
            if self.type == "video" and not self.has_audio:  # only video
                # convert the video to the desired format
                if self.video_instance_file_type != self.format:
                    log.debug("Running ffmpeg on video stream...")
                    subprocess.run([ffmpeg_path, "-i", f"cache/videos/{self.video_id}.{self.video_instance_file_type}", f"cache/videos/{self.video_id}.{self.format}"], env=clean_subprocess_env())
                    log.debug("ffmpeg ran successfully on video")
                    os.remove(f"cache/videos/{self.video_id}.{self.video_instance_file_type}")
                    log.debug("Removed video from cache")
            elif self.type == "audio":  # only audio
                # convert the audio to the desired format
                if self.audio_instance_file_type != self.format:
                    log.debug("Running ffmpeg on audio stream...")
                    subprocess.run([ffmpeg_path, "-i", f"cache/audios/{self.video_id}.{self.audio_instance_file_type}", f"cache/audios/{self.video_id}.{self.format}"], env=clean_subprocess_env())
                    log.debug("ffmpeg ran successfully on audio")
                    os.remove(f"cache/audios/{self.video_id}.{self.audio_instance_file_type}")
                    log.debug("Removed audio from cache")
            
            else:  # video + audio
                # merge the audio and video
                log.debug("Running ffmpeg to merge audio and video streams")
                subprocess.run([ffmpeg_path, "-i", f"cache/videos/{self.video_id}.{self.video_instance_file_type}", "-i", f"cache/audios/{self.video_id}.{self.audio_instance_file_type}", "-c", "copy", f"cache/media/{self.video_id}.{self.format}"], env=clean_subprocess_env())
                log.debug("ffmpeg ran successfully on audio/video merging")
                os.remove(f"cache/videos/{self.video_id}.{self.video_instance_file_type}")
                os.remove(f"cache/audios/{self.video_id}.{self.audio_instance_file_type}")
                log.debug("Removed audio and video from cache")
            
            log.info(f"Successfully processed video {self.video_id} with ffmpeg")
            # get the cache path of the file to move
            cache_file_name = f"{self.video_id}.{self.format}"
            if self.type == "audio":
                output = f"cache/audios/{cache_file_name}"
            elif self.has_audio:
                output = f"cache/media/{cache_file_name}"
            else:
                output = f"cache/videos/{cache_file_name}"
            
            # make sure the file name is not already taken
            while os.path.exists(f"{self.save_path}/{self.file_name}.{self.format}"):
                self.file_name += "_"
            # rename and move the file to the final save path
            log.debug(f"Moving cached file {output} to its destination...")
            destination = f"{self.save_path}/{self.file_name}.{self.format}"
            try:
                try:
                    log.debug("Trying regular rename to move")
                    os.rename(output, destination)
                except:
                    log.debug("Rename failed, trying to move manually")
                    shutil.copyfile(output, destination)
                    os.remove(output)
            except Exception as e:
                log.critical(f"Error when moving processed media {self.video_id} from cache to {destination}: {e}\n")
                raise e
            log.debug("Moved file successfully, end of its processing")
            self.finished.emit()  # send the finished signal to the main thread
    
    
    class VideoInfos(Qt.QWidget):
        """Widget displaying video information and live preview with a checkbox to select it"""

        def __init__(self, video_id:str):
            self.video_id = video_id  # video id
        
        def get_data(self):
            """retrieve all the data from the video, including finding and downloading the channel icon"""
            log.debug(f"Getting preview data for video {self.video_id}")
            try:
                self.video = None
                for _ in range(3):
                    try:
                        self.video = pytube.YouTube.from_id(self.video_id)  # video object
                        break
                    except:
                        pass
                if self.video is None:
                    raise Exception("Failed to load video after 3 attempts")
                self.video_title = self.video.title
                self.channel_name = self.video.author
                self.channel_id = self.video.channel_id
                self.channel_url = self.video.channel_url
                self.embed_url = self.video.embed_url
                self.channel_icon_path = f"cache/channel_icons/{self.channel_id}.jpg"
                self.channel_icon_pixmap = self.channel_icon_pixmap = None
            except:
                log.warning(f"Couldn't get video {self.video_id}")
                self.video = None
                self.video_title = "Error"
                self.channel_name = "Can't get video information"
                self.channel_id = None
                self.embed_url = "https://youtube.com"
                self.channel_icon_path = asset("no_internet.png")
                self.channel_url = "https://youtube.com"
                self.channel_icon_pixmap = self.channel_icon_pixmap = None

        def build_widget(self):
            """builds the widget and its elements"""
            super().__init__()

            self.preview_height = 200
            self.channel_icon_size = 100

            self.big_layout = Qt.QVBoxLayout()
            self.setLayout(self.big_layout)

            # creating the main layout
            self.main_widget = Qt.QWidget()
            self.main_layout = Qt.QHBoxLayout()
            self.main_widget.setLayout(self.main_layout)
            self.big_layout.addWidget(self.main_widget)

            # creating the checkbox
            self.add_button = Qt.QPushButton()
            self.add_button.setFixedSize(30, self.preview_height)
            self.add_button.setIcon(QtGui.QIcon(asset("add.png")))
            self.main_layout.addWidget(self.add_button)

            # creating the video preview
            self.web_view = QtWeb.QWebEngineView()
            self.web_view.setFixedHeight(self.preview_height)
            self.web_view.setFixedWidth(round(16/9*self.preview_height))
            htmlString = ('<style>'
              'html, body { margin:0; padding:0; width:100%; height:100%; overflow:hidden; }'
              'iframe { display:block; width:100%; height:100%; border:0; }'
              '</style>'
              '<iframe src="' + self.embed_url + '" allow="encrypted-media"></iframe>')
            self.web_view.setHtml(htmlString, QtCore.QUrl("https://example.com"))  # needs a real domain for embed
            self.main_layout.addWidget(self.web_view)

            # creating the infos layout
            self.infos_widget = Qt.QWidget()
            self.infos_layout = Qt.QVBoxLayout()
            self.infos_widget.setLayout(self.infos_layout)
            self.main_layout.addWidget(self.infos_widget)

            # creating the video title label
            self.title_label = Qt.QLabel(self.video_title)
            self.title_label.setWordWrap(True)
            self.title_label.setFont(QtGui.QFont("Arial", 20))
            self.title_label.setAlignment(QtCore.Qt.AlignCenter)
            self.infos_layout.addWidget(self.title_label)

            # creating the channel layout
            self.channel_widget = Qt.QWidget()
            self.channel_layout = Qt.QHBoxLayout()
            self.channel_widget.setLayout(self.channel_layout)
            self.infos_layout.addWidget(self.channel_widget)
            self.channel_layout.addStretch()

            # creating the channel label
            self.channel_label = Qt.QLabel(self.channel_name)
            self.channel_label.setWordWrap(True)
            self.channel_label.setFont(QtGui.QFont("Arial", 18))
            self.channel_label.setAlignment(QtCore.Qt.AlignCenter)
            self.channel_layout.addWidget(self.channel_label)
            
            # creating the channel icon
            self.channel_icon = Qt.QLabel()
            self.channel_icon_pixmap = QtGui.QPixmap(asset("profile.png"))
            self.channel_icon_pixmap = self.channel_icon_pixmap.scaled(self.channel_icon_size, self.channel_icon_size, QtCore.Qt.KeepAspectRatio)
            self.channel_icon.setPixmap(self.channel_icon_pixmap)
            self.channel_layout.addWidget(self.channel_icon)

            # creating the separator
            self.separator = Qt.QFrame()
            self.separator.setFrameShape(Qt.QFrame.HLine)
            self.separator.setFrameShadow(Qt.QFrame.Sunken)
            self.big_layout.addWidget(self.separator)
            
            log.debug(f"Built preview widget for video {self.video_id}")
        
        def get_channel_icon_url(self) -> str:
            """finds the channel icon URL from the channel page"""
            response = requests.get(self.channel_url)  # get the channel page html content
            soup = BeautifulSoup(response.text, 'html.parser')  # parse the html content
            meta_tag = soup.find('meta', attrs={'property': 'og:image'})  # find the meta tag with the channel icon URL
            if meta_tag:
                self.channel_icon_url = meta_tag['content']  # get the channel icon URL
                return self.channel_icon_url
            else:
                return "https://static.thenounproject.com/png/2247019-200.png"  # not found
        
        def download_channel_icon(self, path:str):
            """downloads the channel icon in the given folder"""
            if self.channel_id is None:
                return  # error on video gathering
            if not os.path.exists(path):
                os.makedirs(path)  # create the destination folder if it doesn't exist
            image_url = self.get_channel_icon_url()  # get the channel icon URL
            image_data = requests.get(image_url).content  # get the image data
            with open(f"{path}/{self.channel_id}.jpg", "wb") as image_file:
                image_file.write(image_data)  # write the image data in the file
        
        def apply_channel_icon(self):
            """download and display the channel icon"""
            try:
                self.download_channel_icon("cache/channel_icons")
            except requests.exceptions.ConnectionError:
                self.channel_icon_path = asset("no_internet.png")
            self.channel_icon_pixmap = QtGui.QPixmap(self.channel_icon_path)
            self.channel_icon_pixmap = self.channel_icon_pixmap.scaled(self.channel_icon_size, self.channel_icon_size, QtCore.Qt.KeepAspectRatio)
            self.channel_icon.setPixmap(self.channel_icon_pixmap)
    

    class VideoInfosThread(QThread):
        """Thread to load the video infos widgets in the background after a search"""
        video_loaded = pyqtSignal(object)  # signal to send the video infos widget one by one
        finished = pyqtSignal()  # signal to send when the thread is finished

        def __init__(self, search_query:str):
            super().__init__()
            log.debug("Init videos info thread")
            self.running = True  # is the thread running
            self.search_query = search_query  # search query

        def run(self):
            """code to run in the thread, search for the videos and send them one by one to the main thread"""
            try:
                self.search = pytube.Search(self.search_query)  # search for the videos
                self.search_results = self.search.results  # get the search results list

                for result in self.search_results:  # for each result
                    if type(result) != pytube.YouTube:  # skip if not a video
                        continue
                    result_id = result.video_id
                    self.preview = YTDownloader.VideoInfos(result_id)  # create a video infos widget
                    self.preview.get_data()  # load the required data for the video infos widget
                    if self.running:
                        self.video_loaded.emit(self.preview)  # send the video infos widget, it will be build in the main thread to avoid errors
                        log.debug(f"Loaded info for video {result_id}")
                    else:
                        return
                self.finished.emit()  # send the finished signal only if the thread is not stopped
            
            except urllib.error.URLError as e:  # if there is no internet
                log.error(f"Couldn't search for videos: urllib error: {e.reason}")
                self.search_results = []
                self.preview = YTDownloader.VideoInfos("00000000000")
                self.preview.get_data()
                self.video_loaded.emit(self.preview)  # if there is no internet, seld an "empty" video infos widget
                self.finished.emit()
        
        def stop(self):
            """stops the thread"""
            self.running = False
    

    class DownloadInfos(Qt.QWidget):
        """Widget displaying a preview of the videos that are going to be downloaded and allows to change the file name"""
        
        def __init__(self, video_id:str):
            self.video_id = video_id
        
        def get_data(self):
            self.video = pytube.YouTube.from_id(self.video_id)  # video object
            self.thumbnail_height = 150
            self.video_thumbnail = self.video.thumbnail_url  # video thumbnail URL
            
            try:
                self.video_title = self.video.title  # video title
                self.channel_name = self.video.author  # channel name
                self.download_video_thumbnail("cache/thumbnails")  # download the video thumbnail in the cache folder
                self.thumbnail_path = f"cache/thumbnails/{self.video_id}.jpg"  # thumbnail path
            except urllib.error.URLError as e:
                log.warning(f"Couldn't get info for video to download {self.video_id}: {e.reason}")
                self.video_title = "No internet"
                self.channel_name = "Check your network connection"
                self.thumbnail_path = asset("no_internet.png")

        def build_widget(self):
            super().__init__()

            self.big_layout = Qt.QVBoxLayout()
            self.setLayout(self.big_layout)

            # creating the main layout
            self.main_widget = Qt.QWidget()
            self.main_layout = Qt.QHBoxLayout()
            self.main_widget.setLayout(self.main_layout)
            self.big_layout.addWidget(self.main_widget)

            # creating the checkbox
            self.remove_button = Qt.QPushButton()
            self.remove_button.setFixedSize(30, round(self.thumbnail_height*0.6))
            self.remove_button.setIcon(QtGui.QIcon(asset("remove.png")))
            self.main_layout.addWidget(self.remove_button)

            # creating the thumbnail
            self.thumbnail = Qt.QLabel()
            self.thumbnail_pixmap = QtGui.QPixmap(self.thumbnail_path)
            self.thumbnail_pixmap = self.thumbnail_pixmap.scaled(self.thumbnail_height, int(16/9*self.thumbnail_height), QtCore.Qt.KeepAspectRatio)
            self.thumbnail.setPixmap(self.thumbnail_pixmap)
            self.main_layout.addWidget(self.thumbnail)

            # creating the text layout
            self.text_widget = Qt.QWidget()
            self.text_layout = Qt.QVBoxLayout()
            self.text_widget.setLayout(self.text_layout)
            self.main_layout.addWidget(self.text_widget)

            # creating the file name field
            self.file_name = Qt.QLineEdit(self.video_title)
            self.file_name.setFont(QtGui.QFont("Arial", 14))
            self.text_layout.addWidget(self.file_name)

            # creating the video title label
            self.title_label = Qt.QLabel(self.video_title)
            self.title_label.setWordWrap(True)
            self.title_label.setFont(QtGui.QFont("Arial", 14))
            self.text_layout.addWidget(self.title_label)

            # creating the channel label
            self.channel_label = Qt.QLabel(self.channel_name)
            self.channel_label.setWordWrap(True)
            self.channel_label.setFont(QtGui.QFont("Arial", 12))
            self.text_layout.addWidget(self.channel_label)

            # creating the separator
            self.separator = Qt.QFrame()
            self.separator.setFrameShape(Qt.QFrame.HLine)
            self.separator.setFrameShadow(Qt.QFrame.Sunken)
            self.big_layout.addWidget(self.separator)
            
            log.debug(f"Built widget for download info for video {self.video_id}")
        
        def download_video_thumbnail(self, path:str):
            """downloads the video thumbnail in the given folder"""
            if not os.path.exists(path):
                os.makedirs(path)  # create the destination folder if it doesn't exist
            image_data = requests.get(self.video_thumbnail).content  # get the image data
            image = Image.open(io.BytesIO(image_data))  # open the image as an image object

            # crop the image to 16:9 ratio
            width, height = image.size
            new_width, new_height = width, int(width * 9/16)
            if new_height > height:
                new_height, new_width = height, int(new_height * 16/9)
            left = (width - new_width) / 2
            top = (height - new_height) / 2
            right = (width + new_width) / 2
            bottom = (height + new_height) / 2

            image = image.crop((left, top, right, bottom))
            image.save(f"{path}/{self.video_id}.jpg", "JPEG")  # save the image in the file
            log.debug(f"Downloaded thumbnail for video {self.video_id}")
    

    class DownloadInfosThread(QThread):
        """Thread to load the download info widgets in the background after selecting a video"""
        finished = pyqtSignal(object)  # signal to send when the thread is finished with the download infos widgets

        def __init__(self, video_id:str):
            super().__init__()
            self.video_id = video_id  # video id

        def run(self):
            """code to run in the thread, load the download infos widget and send it to the main thread"""
            self.preview = YTDownloader.DownloadInfos(self.video_id)  # create a download infos widget
            self.preview.get_data()  # load the required data for the widget
            self.finished.emit(self.preview)  # send the finished signal with the widget which will have every required data already loaded


    class FfmpegDownloadDialog(Qt.QDialog):
        """Blocking, non-closable popup shown while ffmpeg is being downloaded, with a progress bar"""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Downloading ffmpeg")
            self.setWindowIcon(QtGui.QIcon(asset("icon.ico")))
            self.setModal(True)  # blocks interaction with the main window
            self.setFixedSize(420, 130)
            # remove the close (X) button so the user can't dismiss it manually
            self.setWindowFlags(QtCore.Qt.Dialog | QtCore.Qt.CustomizeWindowHint | QtCore.Qt.WindowTitleHint)

            self.layout_ = Qt.QVBoxLayout()
            self.setLayout(self.layout_)

            self.label = Qt.QLabel("Downloading ffmpeg, please wait...")
            self.label.setWordWrap(True)
            self.label.setFont(QtGui.QFont("Arial", 12))
            self.label.setAlignment(QtCore.Qt.AlignCenter)
            self.layout_.addWidget(self.label)

            self.progress_bar = Qt.QProgressBar()
            self.progress_bar.setValue(0)
            self.layout_.addWidget(self.progress_bar)

            self.size_label = Qt.QLabel("")
            self.size_label.setAlignment(QtCore.Qt.AlignCenter)
            self.layout_.addWidget(self.size_label)

        def update_progress(self, downloaded:int, total:int):
            """update the progress bar and size label, falling back to an indeterminate bar if the size is unknown"""
            if total > 0:
                self.progress_bar.setMaximum(total)
                self.progress_bar.setValue(downloaded)
                self.size_label.setText(f"{YTDownloader.standard_size(self, downloaded)} / {YTDownloader.standard_size(self, total)}")
            else:
                self.progress_bar.setMaximum(0)  # indeterminate/"busy" progress bar
                self.size_label.setText(YTDownloader.standard_size(self, downloaded))

        def closeEvent(self, event):
            """prevent the user from closing the popup while the download is in progress"""
            event.ignore()


    class FfmpegDownloadThread(QThread):
        """Downloads and extracts the ffmpeg binary in the background so the UI thread never blocks"""
        progress = pyqtSignal("qint64", "qint64")  # downloaded bytes, total bytes (64 bit integers)
        finished = pyqtSignal()
        error = pyqtSignal(str)

        def run(self):
            try:
                self.download_ffmpeg()
                self.finished.emit()
            except Exception as e:
                log.error(f"Failed to download ffmpeg: {e}")
                self.error.emit(str(e))

        def download_ffmpeg(self):
            """download the ffmpeg binary in the ffmpeg folder and signal progress"""
            log.info("Downloading ffmpeg binary")
            if not os.path.exists("ffmpeg/"):
                os.makedirs("ffmpeg/")
            if sys.platform == "win32":  # windows
                url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
                archive_path = "ffmpeg/ffmpeg.zip"
            else:  # linux and mac
                url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
                archive_path = "ffmpeg/ffmpeg.tar.xz"

            self.download_file(url, archive_path)

            if sys.platform == "win32":
                self.extract_zip(archive_path)
            else:
                self.extract_tar(archive_path)
            os.remove(archive_path)
            log.info("Downloaded ffmpeg binary")

        def download_file(self, url:str, destination:str):
            """stream the file to disk while emitting progress signals"""
            with requests.get(url, stream=True, timeout=30) as response:
                response.raise_for_status()
                total_size = int(response.headers.get("content-length", 0))
                downloaded = 0
                with open(destination, "wb") as f:
                    for chunk in response.iter_content(chunk_size=1024*1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            self.progress.emit(downloaded, total_size)

        def extract_zip(self, zip_path:str):
            """extract the ffmpeg zip archive on windows, flattening the top-level version folder"""
            with zipfile.ZipFile(zip_path, "r") as archive:
                files = [name for name in archive.namelist() if name and not name.endswith("/")]
                top_level_dirs = {name.split("/", 1)[0] for name in files if "/" in name}

                if len(top_level_dirs) == 1 and all(name.startswith(next(iter(top_level_dirs)) + "/") for name in files):
                    root_dir = next(iter(top_level_dirs)) + "/"
                    for member in archive.infolist():
                        if not member.filename.startswith(root_dir):
                            continue

                        relative_path = member.filename[len(root_dir):]
                        if not relative_path:
                            continue

                        destination = os.path.join("ffmpeg", relative_path)
                        if member.is_dir():
                            os.makedirs(destination, exist_ok=True)
                        else:
                            os.makedirs(os.path.dirname(destination), exist_ok=True)
                            with archive.open(member) as source, open(destination, "wb") as target:
                                target.write(source.read())
                else:
                    archive.extractall("ffmpeg")

        def extract_tar(self, tar_path:str):
            """extract the ffmpeg tar.xz archive on linux/mac, stripping the top-level version folder (equivalent to tar --strip-components=1)"""
            with tarfile.open(tar_path, "r:xz") as archive:
                for member in archive.getmembers():
                    parts = member.name.split("/", 1)
                    if len(parts) < 2 or not parts[1]:
                        continue  # skip the top-level directory entry itself
                    member.name = parts[1]  # strip the first path component
                    archive.extract(member, "ffmpeg")


    def start(self):
        """creates UI and launches the interactive GUI"""
        log.debug("Starting downloader")
        super().__init__()  # initialize the UI module
        self.setWindowTitle("YouTube Downloader")  # set the window title
        self.setWindowIcon(QtGui.QIcon(asset("icon.ico")))  # set the window icon
        self.build_ui()  # build the UI
        log.debug("Built app UI")
        self.setup_software()  # setup the UI, the events and the variables
        log.debug("Have set up the app")
        self.showMaximized()  # maximize the window
        self.show()  # display the UI
        log.debug("Displaying app")
        if not os.path.exists("ffmpeg/ffmpeg") and not os.path.exists("ffmpeg/bin/ffmpeg.exe"):  # if ffmpeg is not installed
            self.download_ffmpeg()
    
    
    def download_ffmpeg(self):
        """show a blocking popup with a progress bar while ffmpeg is downloaded and extracted in the background"""
        self.ffmpeg_dialog = self.FfmpegDownloadDialog(self)
        self.ffmpeg_thread = self.FfmpegDownloadThread()
        self.ffmpeg_thread.progress.connect(self.ffmpeg_dialog.update_progress)
        self.ffmpeg_thread.finished.connect(self.ffmpeg_dialog.accept)  # close the popup once the download is done
        self.ffmpeg_thread.error.connect(self.on_ffmpeg_download_error)
        self.ffmpeg_thread.start()
        self.ffmpeg_dialog.exec_()  # blocks the rest of the UI, keeps the app responsive since the download runs in the thread


    def on_ffmpeg_download_error(self, message:str):
        """close the popup and warn the user if the ffmpeg download failed"""
        log.error(f"ffmpeg download failed: {message}")
        self.ffmpeg_dialog.reject()
        Qt.QMessageBox.critical(self, "Error", f"Failed to download ffmpeg:\n{message}\n\nThe app will not work until ffmpeg is installed.\nTry checking your internet and restart the app.")

    
    def build_ui(self):
        """creates the UI base layout and widgets"""

        # creating base layout
        self.central_widget = Qt.QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = Qt.QHBoxLayout(self.central_widget)

        # creating secondary layouts
        self.videos_layout = Qt.QVBoxLayout()
        self.settings_layout = Qt.QVBoxLayout()
        self.download_layout = Qt.QVBoxLayout()

        # creating corresponding widgets an setting their layout
        self.videos_zone = Qt.QWidget()
        self.settings_tab = Qt.QTabWidget()
        self.download_widget = Qt.QWidget()

        # setting the layouts
        self.videos_zone.setLayout(self.videos_layout)
        self.settings_tab.setLayout(self.settings_layout)
        self.download_widget.setLayout(self.download_layout)

        # creating splitter and adding the layouts
        self.splitter = Qt.QSplitter(1)  # create a horizontal splitter
        self.main_layout.addWidget(self.splitter)
        self.splitter.addWidget(self.videos_zone)
        self.splitter.addWidget(self.settings_tab)
        self.splitter.addWidget(self.download_widget)
        self.splitter.setSizes([500, 200, 300])  # set the size ratio of each section

        # creating items and scrollbox for the video layout
        self.last_search_time = time.time()  # last time a search was made to implement a cooldown
        self.search_widget = Qt.QWidget()
        self.search_layout = Qt.QHBoxLayout()
        self.search_widget.setLayout(self.search_layout)
        self.videos_layout.addWidget(self.search_widget)

        self.searchbar = Qt.QLineEdit()
        self.searchbar.setPlaceholderText("Search for a video")
        self.searchbar.setFixedHeight(30)
        self.searchbar.setFont(QtGui.QFont("Arial", 16))
        self.search_layout.addWidget(self.searchbar)

        self.search_button = Qt.QPushButton()
        self.search_button.setFixedSize(50, 30)
        self.search_button.setIcon(QtGui.QIcon(asset("search.png")))
        self.search_layout.addWidget(self.search_button)

        self.videos_scroll = Qt.QScrollArea()
        self.videos_scroll.setWidgetResizable(True)
        self.videos_scroll.setStyleSheet("QScrollArea { border: none; }")
        self.videos_scroll_inner_widget = Qt.QWidget()
        self.videos_scroll_layout = Qt.QVBoxLayout()
        self.videos_scroll.setWidget(self.videos_scroll_inner_widget)
        self.videos_scroll_inner_widget.setLayout(self.videos_scroll_layout)
        self.videos_layout.addWidget(self.videos_scroll)

        self.videos_scroll_layout.addStretch()

        # creating items for the settings layout
        self.settings_tab.setStyleSheet("QTabBar::tab { font-size: 16px; width: 80px; height: 30px; }")

        self.video_tab = Qt.QWidget()
        self.settings_tab.addTab(self.video_tab, "Video")
        self.settings_video_tab = Qt.QVBoxLayout(self.video_tab)
        self.settings_video = Qt.QVBoxLayout()
        self.settings_video_scroll = Qt.QScrollArea()
        self.settings_video_scroll.setWidgetResizable(True)
        self.settings_video_scroll.setStyleSheet("QScrollArea { border: none; }")
        self.settings_video_scroll_inner_widget = Qt.QWidget()
        self.settings_video_scroll.setWidget(self.settings_video_scroll_inner_widget)
        self.settings_video_scroll_inner_widget.setLayout(self.settings_video)
        self.settings_video_tab.addWidget(self.settings_video_scroll)

        self.audio_tab = Qt.QWidget()
        self.settings_tab.addTab(self.audio_tab, "Audio")
        self.settings_audio_tab = Qt.QVBoxLayout(self.audio_tab)
        self.settings_audio = Qt.QVBoxLayout()
        self.settings_audio_scroll = Qt.QScrollArea()
        self.settings_audio_scroll.setWidgetResizable(True)
        self.settings_audio_scroll.setStyleSheet("QScrollArea { border: none; }")
        self.settings_audio_scroll_inner_widget = Qt.QWidget()
        self.settings_audio_scroll.setWidget(self.settings_audio_scroll_inner_widget)
        self.settings_audio_scroll_inner_widget.setLayout(self.settings_audio)
        self.settings_audio_tab.addWidget(self.settings_audio_scroll)
        
        # video settings
        self.video_hasAudio_box = Qt.QGroupBox("Audio")
        self.video_hasAudio_box.setFont(QtGui.QFont("Arial", 16))
        self.video_hasAudio_layout = Qt.QVBoxLayout()
        self.video_hasAudio_box.setLayout(self.video_hasAudio_layout)
        self.settings_video_hasAudio = Qt.QCheckBox("Include audio")
        self.settings_video_hasAudio.setChecked(True)
        self.video_hasAudio_layout.addWidget(self.settings_video_hasAudio)
        self.settings_video.addWidget(self.video_hasAudio_box)

        self.settings_video_quality = Qt.QButtonGroup()
        self.video_qualities = ["Max", "2160p 4K", "1440p 2K", "1080p FHD", "720p HD", "480p SD", "360p", "240p", "144p"]
        self.video_quality_box = Qt.QGroupBox("Video quality")
        self.video_quality_box.setFont(QtGui.QFont("Arial", 16))
        self.video_quality_layout = Qt.QVBoxLayout()
        self.video_quality_box.setLayout(self.video_quality_layout)
        buttons = [Qt.QRadioButton(quality) for quality in self.video_qualities]
        buttons[0].setChecked(True)
        for button in buttons:
            self.settings_video_quality.addButton(button)
            self.video_quality_layout.addWidget(button)
        self.settings_video.addWidget(self.video_quality_box)

        self.settings_video_format = Qt.QButtonGroup()
        self.video_formats = ["mp4", "mkv", "mov", "avi", "webm"]
        self.video_format_box = Qt.QGroupBox("Video format")
        self.video_format_box.setFont(QtGui.QFont("Arial", 16))
        self.video_format_layout = Qt.QVBoxLayout()
        self.video_format_box.setLayout(self.video_format_layout)
        buttons = [Qt.QRadioButton(format) for format in self.video_formats]
        buttons[0].setChecked(True)
        for button in buttons:
            self.settings_video_format.addButton(button)
            self.video_format_layout.addWidget(button)
        self.settings_video.addWidget(self.video_format_box)

        self.settings_video.addStretch()
        
        # audio settings
        self.settings_audio_format = Qt.QButtonGroup()
        self.audio_formats = ["mp3", "wav", "m4a", "flac", "ogg"]
        self.audio_format_box = Qt.QGroupBox("Audio format")
        self.audio_format_box.setFont(QtGui.QFont("Arial", 16))
        self.audio_format_layout = Qt.QVBoxLayout()
        self.audio_format_box.setLayout(self.audio_format_layout)
        buttons = [Qt.QRadioButton(format) for format in self.audio_formats]
        buttons[0].setChecked(True)
        for button in buttons:
            self.settings_audio_format.addButton(button)
            self.audio_format_layout.addWidget(button)
        self.settings_audio.addWidget(self.audio_format_box)

        self.settings_audio.addStretch()

        # download section
        self.download_list_layout = Qt.QVBoxLayout()
        self.download_list = Qt.QScrollArea()
        self.download_list.setStyleSheet("QScrollArea { border: none; }")
        self.download_list.setWidgetResizable(True)
        self.download_inner_widget = Qt.QWidget()
        self.download_list.setWidget(self.download_inner_widget)
        self.download_inner_widget.setLayout(self.download_list_layout)
        self.download_layout.addWidget(self.download_list)
        
        self.download_button_widget = Qt.QWidget()
        self.download_button_layout = Qt.QVBoxLayout()
        self.download_button_widget.setLayout(self.download_button_layout)
        self.download_layout.addWidget(self.download_button_widget)

        self.add_video_widget = Qt.QWidget()
        self.add_video_layout = Qt.QHBoxLayout()
        self.add_video_widget.setLayout(self.add_video_layout)
        self.download_button_layout.addWidget(self.add_video_widget)

        self.add_video_field = Qt.QLineEdit()
        self.add_video_field.setPlaceholderText("Add video from URL")
        self.add_video_field.setFixedHeight(30)
        self.add_video_field.setFont(QtGui.QFont("Arial", 14))
        self.add_video_layout.addWidget(self.add_video_field)

        self.add_video_button = Qt.QPushButton()
        self.add_video_button.setFixedSize(30, 30)
        self.add_video_button.setIcon(QtGui.QIcon(asset("add.png")))
        self.add_video_layout.addWidget(self.add_video_button)

        self.download_button = Qt.QPushButton("Download")
        self.download_button.setFixedHeight(50)
        self.download_button.setFont(QtGui.QFont("Arial", 20))
        self.download_button_layout.addWidget(self.download_button)
        
        self.download_list_layout.addStretch()
    
    def setup_software(self):
        """sets up the UI, the events and the variables"""
        self.selected_videos = []  # ids of the selected videos
        self.total_file_size = 0  # total size of the selected videos in bytes
        self.search_display_thread = None  # thread to display the search results

        self.searchbar.returnPressed.connect(self.search_video)  # search when pressing enter
        self.search_button.clicked.connect(self.search_video)  # search when clicking the search button
        self.add_video_field.returnPressed.connect(self.add_video_from_url)  # add a video when pressing enter in the add video field
        self.add_video_button.clicked.connect(self.add_video_from_url)  # add a video when clicking the add video button
        self.download_button.clicked.connect(self.download_selected_videos)  # download the selected videos when clicking the download button
    
    def download_selected_videos(self):
        """downloads the selected videos with the selected settings after asking for the save folder"""
        if not self.selected_videos:
            return
        downloads_default = os.path.join(os.path.expanduser("~"), "Downloads")
        self.save_path = Qt.QFileDialog.getExistingDirectory(self, "Select download location", downloads_default)  # ask for the save folder
        if not self.save_path:
            return
        
        if self.settings_tab.currentIndex() == 0:
            self.type = "video"
            self.settings = {
                "quality": self.settings_video_quality.checkedButton().text(),
                "has_audio": self.settings_video_hasAudio.isChecked(),
                "format": self.settings_video_format.checkedButton().text().lower(),
                "file_name": "",
                "save_path": self.save_path
            }
        else:
            self.type = "audio"
            self.settings = {
                "format": self.settings_audio_format.checkedButton().text().lower(),
                "file_name": "",
                "save_path": self.save_path
            }
        self.init_download_window()
    
    def init_download_window(self):
        """Initialize the download window and starts the downloading process"""
        self.videos_dict = {}  # {video_id: file_name}
        self.file_names = []  # list of file names
        for i in range(self.download_list_layout.count()):
            item = self.download_list_layout.itemAt(i)
            if item:
                widget = item.widget()
                if widget:
                    file_name = widget.file_name.text()
                    self.file_names.append(file_name)
        for i in range(len(self.selected_videos)):
            self.videos_dict[self.selected_videos[i]] = self.file_names[i]
        self.download_window = self.DownloadWindow(self.videos_dict, self.type, self.settings)
        self.download_window.download()
    
    def add_video_from_url(self):
        """adds a video from the URL in the add video field"""
        url = self.add_video_field.text()
        if url:
            try:
                yt = pytube.YouTube(url)
                video_id = yt.video_id
                self.video_add(video_id)
                self.add_video_field.clear()
                log.debug(f"Added video {video_id} as link")
            except (pytube.exceptions.RegexMatchError, pytube.exceptions.VideoUnavailable):
                self.add_video_field.clear()
    
    def search_video(self):
        """searches for a video and displays the results via a thread"""
        current_time = time.time()
        if current_time - self.last_search_time < 1:
            return  # do not search before the cooldown
        
        self.last_search_time = current_time
        if self.search_display_thread:
            self.search_display_thread.stop()  # stop the previous search thread if it exists
        self.clear_layout(self.videos_scroll_layout)  # clear the previous search results
        self.search_query = self.searchbar.text()  # get the search query
        # creating loading label
        self.loading_label = Qt.QLabel("Loading results...")
        self.loading_label.setFont(QtGui.QFont("Arial", 20))
        self.loading_label.setAlignment(QtCore.Qt.AlignCenter)
        self.videos_scroll_layout.insertWidget(self.videos_scroll_layout.count()-1, self.loading_label)  # display the loading label

        self.search_display_thread = self.VideoInfosThread(self.search_query)  # create a new search thread
        self.search_display_thread.video_loaded.connect(self.show_video_preview)  # display the video previews one by one when the thread sends it
        self.search_display_thread.video_loaded.connect(self.load_channel_icon)  # load the channel icons when the preview is loaded
        self.search_display_thread.finished.connect(self.remove_loading_label)  # remove the loading label when the thread is finished
        self.search_display_thread.start()  # start the search thread
        log.info(f"Searching with query '{self.search_query}'")
    
    def show_video_preview(self, preview:VideoInfos):
        """displays a video preview"""
        preview.build_widget()  # build the video preview widget
        self.videos_scroll_layout.insertWidget(self.videos_scroll_layout.count()-2, preview)  # add the video preview widget to the layout
        preview.add_button.clicked.connect(lambda: self.video_add(preview.video_id))  # add the video to the selected videos list when the checkbox is checked or unchecked
    
    def load_channel_icon(self, preview:VideoInfos):
        """launches the thread to download and display the channel icon"""
        self.load_channel_icon_t = thr.Thread(target=self.load_channel_icon_thread, args=(preview,))
        self.load_channel_icon_t.start()  # start the thread
    
    def load_channel_icon_thread(self, preview:VideoInfos):
        """download and display the channel icons on the video previews"""
        preview.apply_channel_icon()  # download and display the channel icon

    def remove_loading_label(self):
        """removes the loading label from the search results if it exists"""
        if self.loading_label:
            try:
                self.videos_scroll_layout.removeWidget(self.loading_label)
                self.loading_label.deleteLater()
            except RuntimeError:
                pass
    
    def video_add(self, video_id:str):
        """adds a video in the selected videos list if it's not already in it"""
        if video_id not in self.selected_videos:
            self.selected_videos.append(video_id)  # add the video id to the selected videos list
            self.add_download_preview(video_id)  # add the video preview to the download list
    
    def add_download_preview(self, video_id:str):
        """adds a video preview to the download list"""
        self.download_infos_thread = self.DownloadInfosThread(video_id)
        self.download_infos_thread.finished.connect(self.show_download_preview)
        self.download_infos_thread.start()
    
    def show_download_preview(self, preview:DownloadInfos):
        """displays a video preview in the download list"""
        preview.build_widget()
        self.download_list_layout.insertWidget(self.download_list_layout.count()-1, preview)
        preview.remove_button.clicked.connect(lambda: self.video_remove(preview.video_id))
    
    def video_remove(self, video_id:str):
        """removes a video from the selected videos list and the download list"""
        self.selected_videos.remove(video_id)
        for i in range(self.download_list_layout.count()):
            item = self.download_list_layout.itemAt(i)
            if item:
                widget = item.widget()
                if widget and widget.video_id == video_id:
                    self.download_list_layout.removeWidget(widget)
                    widget.deleteLater()
    
    def standard_size(self, size:int) -> str:
        """converts a size in bytes to a human readable size"""
        units = ["B", "KB", "MB", "GB", "TB", "PB"]
        unit = 0
        while size >= 1024:
            size /= 1024
            unit += 1
            if unit == len(units)-1:
                break
        return f"{round(size, 2)} {units[unit]}"

    def clear_layout(self, layout:Qt.QLayout):
        """Clears a layout"""
        while layout.count() > 1:
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:  # if the item is a widget
                widget.deleteLater()  # delete the widget


def clear_cache():
    """clears the cache folder"""
    cache = glob.glob("cache/*.*") + glob.glob("cache/*/*.*")
    for f in cache:
        try:
            os.remove(f)
        except Exception as e:
            log.error(f"Can't remove cached file {f}: {e}")

def create_cache():
    """creates the cache folder if it doesn't exist"""
    if not os.path.exists("cache"):
        os.makedirs("cache")
    for folder in ["videos", "audios", "media", "thumbnails", "channel_icons"]:
        if not os.path.exists(f"cache/{folder}"):
            os.makedirs(f"cache/{folder}")

if __name__ == "__main__":
    log.info("Starting the app")
    try:
        create_cache()
        log.debug("Created cache")
        if sys.platform != "win32":
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--no-sandbox"  # set the environment variable for the web engine
            log.debug("Set flags for web engine")
        App = Qt.QApplication(sys.argv)  # creating the app
        App.setStyle("fusion")
        if IS_DARK:
            # dark mode palette
            palette = QtGui.QPalette()
            palette.setColor(QtGui.QPalette.Window, QtGui.QColor(53, 53, 53))
            palette.setColor(QtGui.QPalette.WindowText, QtCore.Qt.white)
            palette.setColor(QtGui.QPalette.Base, QtGui.QColor(25, 25, 25))
            palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(53, 53, 53))
            palette.setColor(QtGui.QPalette.ToolTipBase, QtCore.Qt.black)
            palette.setColor(QtGui.QPalette.ToolTipText, QtCore.Qt.white)
            palette.setColor(QtGui.QPalette.Text, QtCore.Qt.white)
            palette.setColor(QtGui.QPalette.Button, QtGui.QColor(53, 53, 53))
            palette.setColor(QtGui.QPalette.ButtonText, QtCore.Qt.white)
            palette.setColor(QtGui.QPalette.BrightText, QtCore.Qt.red)
            palette.setColor(QtGui.QPalette.Link, QtGui.QColor(42, 130, 218))
            palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(42, 130, 218))
            palette.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.black)
            App.setPalette(palette)
        Window = YTDownloader()  # creating the GUI
        Window.start()  # starting the GUI
        log.info("Starting the window")
        App.exec_()  # executing the app
    except Exception as e:
        log.critical(f"An error occurred: {e}")
    finally:
        # always clear cache
        clear_cache()
        log.info("Cleared cache")
        log.info("Exiting the app\n")
