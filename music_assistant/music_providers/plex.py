"""Plex musicprovider support for MusicAssistant."""
from __future__ import annotations

import datetime
import hashlib
import time
from json import JSONDecodeError
from typing import AsyncGenerator, Set, List, Optional, Tuple

import aiohttp
from asyncio_throttle import Throttler

from plexapi.server import PlexServer

from music_assistant.helpers.app_vars import (  # pylint: disable=no-name-in-module
    app_var,
)
from music_assistant.helpers.util import parse_title_and_version, try_parse_int
from music_assistant.models.enums import MusicProviderFeature, ProviderType
from music_assistant.models.errors import LoginFailed, MediaNotFoundError
from music_assistant.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaItemProviderId,
    MediaItemType,
    MediaQuality,
    MediaType,
    Playlist,
    StreamDetails,
    Track,
)
from music_assistant.models.music_provider import MusicProvider


class PlexProvider(MusicProvider):
    """Provider for Plex Servers."""

    _attr_type = ProviderType.PLEX
    _attr_name = "Plex"
    _plex_server = None
    _plex_music = None
    _url = ''
    _token = ''
    _throttler = Throttler(rate_limit=4, period=1)
    _uids = {}

    @property
    def supported_features(self) -> Tuple[MusicProviderFeature]:
        """Return the features supported by this MusicProvider."""
        return (
            MusicProviderFeature.LIBRARY_ARTISTS,
            MusicProviderFeature.LIBRARY_ALBUMS,
            MusicProviderFeature.LIBRARY_TRACKS,
            MusicProviderFeature.LIBRARY_PLAYLISTS,
            MusicProviderFeature.LIBRARY_ARTISTS_EDIT,
            MusicProviderFeature.LIBRARY_ALBUMS_EDIT,
            MusicProviderFeature.LIBRARY_PLAYLISTS_EDIT,
            MusicProviderFeature.LIBRARY_TRACKS_EDIT,
            MusicProviderFeature.PLAYLIST_TRACKS_EDIT,
            MusicProviderFeature.BROWSE,
            MusicProviderFeature.SEARCH,
            MusicProviderFeature.ARTIST_ALBUMS,
            MusicProviderFeature.ARTIST_TOPTRACKS,
        )

    async def setup(self) -> bool:
        """Handle async initialization of the provider."""
        if not self.config.enabled:
            return False
        if not self.config.username or not self.config.password:
            raise LoginFailed("Invalid login credentials")

        # For now, we only support URL/Token access to the Plex Server
        self._url = self.config.username
        self._token = self.config.password

        # try to get a token, raise if that fails
        server = await self._get_server()
        if not server:
            raise LoginFailed(f"Login failed for user {self.config.username}")
        return True

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> List[MediaItemType]:
        """
        Perform search on musicprovider.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: Number of items to return in the search (per type).
        """
        result = []
        params = {"query": search_query, "limit": limit}
        if len(media_types) == 1:
            # plex does not support multiple searchtypes, falls back to all if no type given
            if media_types[0] == MediaType.ARTIST:
                params["type"] = "artists"
            if media_types[0] == MediaType.ALBUM:
                params["type"] = "albums"
            if media_types[0] == MediaType.TRACK:
                params["type"] = "tracks"
            if media_types[0] == MediaType.PLAYLIST:
                params["type"] = "playlists"
        if searchresult := await self._get_data("catalog/search", **params):
            if "artists" in searchresult:
                result += [
                    await self._process_artist(item)
                    for item in searchresult["artists"]["items"]
                    if (item and item["id"])
                ]
            if "albums" in searchresult:
                result += [
                    await self._parse_album(item)
                    for item in searchresult["albums"]["items"]
                    if (item and item["id"])
                ]
            if "tracks" in searchresult:
                result += [
                    await self._process_track(item)
                    for item in searchresult["tracks"]["items"]
                    if (item and item["id"])
                ]
            if "playlists" in searchresult:
                result += [
                    await self._parse_playlist(item)
                    for item in searchresult["playlists"]["items"]
                    if (item and item["id"])
                ]
        return result

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Plex."""
        for artist in self._plex_music.searchArtists():
            yield await self._process_artist(artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Plex."""
        albums = self._plex_music.searchAlbums()
        print('Getting albums: ',len(albums))
        for plex_album in albums:
            yield await self._process_album(plex_album)

    async def get_library_albums_parsing(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Plex."""
        albums = self._plex_music.searchAlbums()
        print('Getting albums: ',len(albums))
        for plex_album in self._plex_music.searchAlbums():
            yield await self._parse_album(plex_album)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Plex."""
        tracks = self._plex_music.searchTracks()
        print('Getting tracks: ',len(tracks))
        for plex_track in []: # self._plex_music.searchTracks():
            yield await self._process_track(plex_track)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlists = self._plex_music.playlists()
        for playlist in playlists:
            yield await self._parse_playlist(playlist)

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        plex_artist = self._plex_music.fetchItem(int(prov_artist_id))
        return (
            await self._process_artist(plex_artist)
            if plex_artist
            else None
        )

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        plex_album = self._plex_music.fetchItem(int(prov_album_id))
        return (
            await self._process_album(plex_album)
            if plex_album
            else None
        )

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        print("Getting track ID: ", prov_track_id)
        plex_track = self._plex_music.fetchItem(int(prov_track_id))
        return (
            await self._process_track(plex_track)
            if plex_track
            else None
        )

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        plex_playlist = self._plex_music.fetchItem(int(prov_playlist_id))
        print ("get_playlist: ", plex_playlist)
        return (
            await self._parse_playlist(plex_playlist)
            if plex_playlist
            else None
        )

    async def get_album_tracks(self, prov_album_id) -> List[Track]:
        """Get all album tracks for given album id."""
        params = {"album_id": prov_album_id}
        return [
            await self._process_track(item)
            for item in await self._get_all_items("album/get", **params, key="tracks")
            if (item and item["id"])
        ]

    async def get_playlist_tracks(self, prov_playlist_id) -> List[Track]:
        """Get all playlist tracks for given playlist id."""
        plex_playlist = self._plex_music.fetchItem(int(prov_playlist_id))
        return [
            await self._process_track(item)
            for item in plex_playlist.items()
            if (item)
        ]

    async def get_artist_albums(self, prov_artist_id) -> List[Album]:
        """Get a list of albums for the given artist."""
        endpoint = "artist/get"
        return [
            await self._parse_album(item)
            for item in await self._get_all_items(
                endpoint, key="albums", artist_id=prov_artist_id, extra="albums"
            )
            if (item and item["id"] and str(item["artist"]["id"]) == prov_artist_id)
        ]

    async def get_artist_toptracks(self, prov_artist_id) -> List[Track]:
        """Get a list of most popular tracks for the given artist."""
        result = await self._get_data(
            "artist/get",
            artist_id=prov_artist_id,
            extra="playlists",
            offset=0,
            limit=25,
        )
        if result and result["playlists"]:
            return [
                await self._process_track(item)
                for item in result["playlists"][0]["tracks"]["items"]
                if (item and item["id"])
            ]
        # fallback to search
        artist = await self.get_artist(prov_artist_id)
        searchresult = await self._get_data(
            "catalog/search", query=artist.name, limit=25, type="tracks"
        )
        return [
            await self._process_track(item)
            for item in searchresult["tracks"]["items"]
            if (
                item
                and item["id"]
                and "performer" in item
                and str(item["performer"]["id"]) == str(prov_artist_id)
            )
        ]

    async def get_similar_artists(self, prov_artist_id):
        """Get similar artists for given artist."""
        # https://www.plex.com/api.json/0.2/artist/getSimilarArtists?artist_id=220020&offset=0&limit=3

    async def library_add(self, prov_item_id, media_type: MediaType):
        """Add item to library."""
        result = None
        if media_type == MediaType.ARTIST:
            result = await self._get_data("favorite/create", artist_id=prov_item_id)
        elif media_type == MediaType.ALBUM:
            result = await self._get_data("favorite/create", album_ids=prov_item_id)
        elif media_type == MediaType.TRACK:
            result = await self._get_data("favorite/create", track_ids=prov_item_id)
        elif media_type == MediaType.PLAYLIST:
            result = await self._get_data(
                "playlist/subscribe", playlist_id=prov_item_id
            )
        return result

    async def library_remove(self, prov_item_id, media_type: MediaType):
        """Remove item from library."""
        result = None
        if media_type == MediaType.ARTIST:
            result = await self._get_data("favorite/delete", artist_ids=prov_item_id)
        elif media_type == MediaType.ALBUM:
            result = await self._get_data("favorite/delete", album_ids=prov_item_id)
        elif media_type == MediaType.TRACK:
            result = await self._get_data("favorite/delete", track_ids=prov_item_id)
        elif media_type == MediaType.PLAYLIST:
            playlist = await self.get_playlist(prov_item_id)
            if playlist.is_editable:
                result = await self._get_data(
                    "playlist/delete", playlist_id=prov_item_id
                )
            else:
                result = await self._get_data(
                    "playlist/unsubscribe", playlist_id=prov_item_id
                )
        return result

    async def add_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> None:
        """Add track(s) to playlist."""
        return await self._get_data(
            "playlist/addTracks",
            playlist_id=prov_playlist_id,
            track_ids=",".join(prov_track_ids),
            playlist_track_ids=",".join(prov_track_ids),
        )

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> None:
        """Remove track(s) from playlist."""
        playlist_track_ids = set()
        for track in await self._get_all_items(
            "playlist/get", key="tracks", playlist_id=prov_playlist_id, extra="tracks"
        ):
            if str(track["id"]) in prov_track_ids:
                playlist_track_ids.add(str(track["playlist_track_id"]))
        return await self._get_data(
            "playlist/deleteTracks",
            playlist_id=prov_playlist_id,
            playlist_track_ids=",".join(playlist_track_ids),
        )

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        streamdata = None
        for format_id in []: #[27, 7, 6, 5]:
            # it seems that simply requesting for highest available quality does not work
            # from time to time the api response is empty for this request ?!
            result = await self._get_data(
                "track/getFileUrl",
                sign_request=True,
                format_id=format_id,
                track_id=item_id,
                intent="stream",
            )
            if result and result.get("url"):
                streamdata = result
                break
        if not streamdata:
            raise MediaNotFoundError(f"Unable to retrieve stream details for {item_id}")
        if streamdata["mime_type"] == "audio/mpeg":
            content_type = ContentType.MPEG
        elif streamdata["mime_type"] == "audio/flac":
            content_type = ContentType.FLAC
        else:
            raise MediaNotFoundError(f"Unsupported mime type for {item_id}")
        # report playback started as soon as the streamdetails are requested
        self.mass.create_task(self._report_playback_started(item_id, streamdata))
        return StreamDetails(
            item_id=str(item_id),
            provider=self.type,
            content_type=content_type,
            duration=streamdata["duration"],
            sample_rate=int(streamdata["sampling_rate"] * 1000),
            bit_depth=streamdata["bit_depth"],
            data=streamdata,  # we need these details for reporting playback
            expires=time.time() + 1800,  # not sure about the real allowed value
            direct=streamdata["url"],
            callback=self._report_playback_stopped,
        )

    async def _report_playback_started(self, item_id: str, streamdata: dict) -> None:
        """Report playback start to plex."""
        # TODO: need to figure out if the streamed track is purchased by user
        # https://www.plex.com/api.json/0.2/purchase/getUserPurchasesIds?limit=5000&user_id=xxxxxxx
        # {"albums":{"total":0,"items":[]},"tracks":{"total":0,"items":[]},"user":{"id":xxxx,"login":"xxxxx"}}
        device_id = self._plex_server["user"]["device"]["id"]
        credential_id = self._plex_server["user"]["credential"]["id"]
        user_id = self._plex_server["user"]["id"]
        format_id = streamdata["format_id"]
        timestamp = int(time.time())
        events = [
            {
                "online": True,
                "sample": False,
                "intent": "stream",
                "device_id": device_id,
                "track_id": str(item_id),
                "purchase": False,
                "date": timestamp,
                "credential_id": credential_id,
                "user_id": user_id,
                "local": False,
                "format_id": format_id,
            }
        ]
        await self._post_data("track/reportStreamingStart", data=events)

    async def _report_playback_stopped(self, streamdetails: StreamDetails) -> None:
        """Report playback stop to plex."""
        user_id = self._plex_server["user"]["id"]
        await self._get_data(
            "/track/reportStreamingEnd",
            user_id=user_id,
            track_id=str(streamdetails.item_id),
            duration=try_parse_int(streamdetails.seconds_streamed),
        )

    async def _process_artist(self, plex_artist: plexapi.Artist):
        """Parse plex artist object to generic layout."""
        
        uid = str(plex_artist.ratingKey)
        artist = self._uids.get(uid, None)
        if not artist:
            print('Artist: ', uid, plex_artist.title, plex_artist.key, self.id)
            artist = Artist(
                item_id=uid,
                provider=self.type,
                name=plex_artist.title
            )
            artist.add_provider_id(
                MediaItemProviderId(
                    item_id=uid,
                    prov_type=self.type,
                    prov_id=self.id,
                    url=plex_artist.key
                )
            )
#        if img := self.__get_image(plex_artist):
#            artist.metadata.images = [MediaItemImage(ImageType.THUMB, img)]
            artist.metadata.description = plex_artist.summary
            self._uids[uid] = artist
        return artist

    async def _process_album(self, plex_album: plexapi.Album, force = False):
        """Parse Plex album object to generic layout."""
#        if not plex_artist and "artist" not in plex_album:
#            # artist missing in album info, return full abum instead
#            return await self.get_album(plex_album["id"])
        uid = str(plex_album.ratingKey)
        print("_process_album: ",uid)
        album = self._uids.get(uid, None)
        if not album:
            name = plex_album.title
            version = None
            print('_process_album: Album: ', name, uid)
            album = Album(
                item_id=uid, provider=self.type, name=name, version=version
            )
            album.add_provider_id(
                MediaItemProviderId(
                    item_id=uid,
                    prov_type=self.type,
                    prov_id=self.id,
                    quality=None,
                    url=plex_album.key,
                    details="Todo: Album Details?",
                    available=True
                )
            )
            album.album_type = AlbumType.ALBUM
#        album.metadata.genres = Set[str]
#        for genre in plex_album.genres:
#            album.metadata.genres.add(genre.tag)
#        if img := self.__get_image(plex_album):
#            album.metadata.images = [MediaItemImage(ImageType.THUMB, img)]
#        if "label" in plex_album:
#            album.metadata.label = plex_album["label"]["name"]
            if plex_album.year:
                album.year = plex_album.year
            if plex_album.summary:
                album.metadata.description = plex_album.summary
            self._uids[uid] = album

        artist_id = str(plex_album.parentRatingKey)
        print('_process_album: album/artist: ',plex_album.ratingKey,artist_id,self.type,self.id)
 #       print('_uids.length(): ',len(self._uids),self._uids.get(artist_id, None))
        artist = self._uids.get(artist_id, None)
        if not artist:
            artist = await self._process_artist(plex_album.artist())
            print("FOOOOOOOOOOOOOOO",artist.name)
        print("BAAARR",artist.name,artist.item_id)
        album.artist = artist

        return album

    async def _parse_album(self, plex_album: plexapi.Album, plex_artist: plexapi.Artist = None):
        """Parse Plex album object to generic layout."""
#        if not plex_artist and "artist" not in plex_album:
#            # artist missing in album info, return full abum instead
#            return await self.get_album(plex_album["id"])
        uid = str(plex_album.ratingKey)
        print("_parse_album: ", uid)
        album = self._uids.get(uid, None)
        if not album:
            name = plex_album.title
            version = None
            print('_parse_album: Album: ', name)
            album = Album(
                item_id=uid, provider=self.type, name=name, version=version
            )
            album.add_provider_id(
                MediaItemProviderId(
                    item_id=str(uid),
                    prov_type=self.type,
                    prov_id=self.id,
                    quality=None,
                    url=plex_album.key,
                    details="Todo: Album Details?",
                    available=True
                )
            )

            album.artist = await self._process_artist(plex_artist or plex_album.artist())
            album.album_type = AlbumType.ALBUM
#        album.metadata.genres = Set[str]
#        for genre in plex_album.genres:
#            album.metadata.genres.add(genre.tag)
#        if img := self.__get_image(plex_album):
#            album.metadata.images = [MediaItemImage(ImageType.THUMB, img)]
#        if "label" in plex_album:
#            album.metadata.label = plex_album["label"]["name"]
            if plex_album.year:
                album.year = plex_album.year
            if plex_album.summary:
                album.metadata.description = plex_album.summary
            self._uids[uid] = album

        return album

    async def _process_track(self, plex_track: plexapi.Track):
        """Parse plex track object to generic layout."""
        uid = str(plex_track.ratingKey)
        track = self._uids.get(uid, None)
        if not track:
            name = plex_track.title
            media = plex_track.media
            track = Track(
                item_id=uid,
                provider=self.type,
                name=name,
    #            version=version,
    #            disc_number=plex_track["media_number"],
    #            track_number=plex_track["track_number"],
                duration=plex_track.duration,
    #            position=plex_track.get("position"),
            )
            plex_album = plex_track.album()
            album = await self._process_album(plex_album)
            if album:
                track.album = album

            plex_artist = plex_track.artist()
            if (plex_album and not plex_artist):
                plex_artist = plex_album.artist()
            artist = await self._process_artist(plex_artist)
            if artist:
                track.artists.append(artist)

    #        for plex_media
    #        if plex_track.get("copyright"):
    #            track.metadata.copyright = plex_track["copyright"]
    #        if plex_track.get("audio_info"):
    #            track.metadata.replaygain = plex_track["audio_info"]["replaygain_track_gain"]
    #        if plex_track.get("parental_warning"):
    #            track.metadata.explicit = True
    #        if img := self.__get_image(plex_track):
    #            track.metadata.images = [MediaItemImage(ImageType.THUMB, img)]
    #        # get track quality
    #        if plex_track["maximum_sampling_rate"] > 192:
    #            quality = MediaQuality.LOSSLESS_HI_RES_4
    #        elif plex_track["maximum_sampling_rate"] > 96:
    #            quality = MediaQuality.LOSSLESS_HI_RES_3
    #        elif plex_track["maximum_sampling_rate"] > 48:
    #            quality = MediaQuality.LOSSLESS_HI_RES_2
    #        elif plex_track["maximum_bit_depth"] > 16:
    #            quality = MediaQuality.LOSSLESS_HI_RES_1
    #        elif plex_track.get("format_id", 0) == 5:
    #            quality = MediaQuality.LOSSY_AAC
    #        else:
    #            quality = MediaQuality.LOSSLESS
            track.add_provider_id(
                MediaItemProviderId(
                    item_id=uid,
                    prov_type=self.type,
                    prov_id=self.id,
    #                quality=quality,
                    url=plex_track.key,
    #                details=f'{plex_track["maximum_sampling_rate"]}kHz {plex_track["maximum_bit_depth"]}bit',
    #                available=plex_track["streamable"] and plex_track["displayable"],
                )
            )
            self._uids[uid] = track

        return track

    async def _parse_playlist(self, plex_playlist):
        """Parse plex playlist object to generic layout."""
        uid = str(plex_playlist.ratingKey)
        print('Playlist: ', plex_playlist.title)
        playlist = Playlist(
            item_id=uid,
            provider=self.type,
            name=plex_playlist.title
        )
        playlist.add_provider_id(
            MediaItemProviderId(
                item_id=uid,
                prov_type=self.type,
                prov_id=self.id,
                url=plex_playlist.key,
            )
        )
#        playlist.is_editable = True
#        if img := self.__get_image(playlist_obj):
#            playlist.metadata.images = [MediaItemImage(ImageType.THUMB, img)]
        playlist.metadata.checksum = str(plex_playlist.updatedAt)
        return playlist

    async def _get_server (self):
        """Get PlexServer instance."""
        # return existing server if we have one in memory
        if not self._plex_server:
            print('Connecting', self._url, self._token)
            self._plex_server = PlexServer(self._url, self._token)
            self._plex_music = self._plex_server.library.section('Music Test')
        return self._plex_server

    async def _get_track(self, prov_track_id) -> plexapi.Track:
        """Get the Plex track object."""
        fetch_str = ''.join(["//",prov_track_id])
        plex_track = self._plex_music.fetchItems(fetch_str)[0]
        print ("_get_track: ", plex_track)
        return plex_track

    async def _get_all_items(self, endpoint, key="tracks", **kwargs):
        """Get all items from a paged list."""
        limit = 50
        offset = 0
        all_items = []
        while True:
            kwargs["limit"] = limit
            kwargs["offset"] = offset
            result = await self._get_data(endpoint, **kwargs)
            offset += limit
            if not result:
                break
            if not result.get(key) or not result[key].get("items"):
                break
            for item in result[key]["items"]:
                item["position"] = len(all_items) + 1
                all_items.append(item)
            if len(result[key]["items"]) < limit:
                break
        return all_items

    async def _get_data(self, endpoint, sign_request=False, **kwargs):
        """Get data from api."""
        url = f"http://www.plex.com/api.json/0.2/{endpoint}"
        headers = {"X-App-Id": app_var(0)}
        if endpoint != "user/login":
            auth_token = await self._auth_token()
            if not auth_token:
                self.logger.debug("Not logged in")
                return None
            headers["X-User-Auth-Token"] = auth_token
        if sign_request:
            signing_data = "".join(endpoint.split("/"))
            keys = list(kwargs.keys())
            keys.sort()
            for key in keys:
                signing_data += f"{key}{kwargs[key]}"
            request_ts = str(time.time())
            request_sig = signing_data + request_ts + app_var(1)
            request_sig = str(hashlib.md5(request_sig.encode()).hexdigest())
            kwargs["request_ts"] = request_ts
            kwargs["request_sig"] = request_sig
            kwargs["app_id"] = app_var(0)
            kwargs["user_auth_token"] = await self._auth_token()
        async with self._throttler:
            async with self.mass.http_session.get(
                url, headers=headers, params=kwargs, verify_ssl=False
            ) as response:
                try:
                    result = await response.json()
                    if "error" in result or (
                        "status" in result and "error" in result["status"]
                    ):
                        self.logger.error("%s - %s", endpoint, result)
                        return None
                except (
                    aiohttp.ContentTypeError,
                    JSONDecodeError,
                ) as err:
                    self.logger.error("%s - %s", endpoint, str(err))
                    return None
                return result

    async def _post_data(self, endpoint, params=None, data=None):
        """Post data to api."""
        if not params:
            params = {}
        if not data:
            data = {}
        url = f"http://www.plex.com/api.json/0.2/{endpoint}"
        params["app_id"] = app_var(0)
        params["user_auth_token"] = await self._auth_token()
        async with self.mass.http_session.post(
            url, params=params, json=data, verify_ssl=False
        ) as response:
            result = await response.json()
            if "error" in result or (
                "status" in result and "error" in result["status"]
            ):
                self.logger.error("%s - %s", endpoint, result)
                return None
            return result

    def __get_image(self, obj: dict) -> Optional[str]:
        """Try to parse image from Plex media object."""
        if obj.get("image"):
            for key in ["extralarge", "large", "medium", "small"]:
                if obj["image"].get(key):
                    if "2a96cbd8b46e442fc41c2b86b821562f" in obj["image"][key]:
                        continue
                    return obj["image"][key]
        if obj.get("images300"):
            # playlists seem to use this strange format
            return obj["images300"][0]
        if obj.get("album"):
            return self.__get_image(obj["album"])
        if obj.get("artist"):
            return self.__get_image(obj["artist"])
        return None
