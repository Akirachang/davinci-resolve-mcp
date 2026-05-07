import unittest

from src.server import (
    _append_clip_info_from_timeline_item,
    _find_appended_timeline_item_summary,
    _serialize_appended_timeline_item,
)


class TimelineItemStub:
    def __init__(self, unique_id="timeline-item-123", name="synthetic_append_clip_infos.mp4"):
        self.unique_id = unique_id
        self.name = name

    def GetUniqueId(self):
        return self.unique_id

    def GetName(self):
        return self.name


class BrokenTimelineItemStub:
    def GetUniqueId(self):
        raise RuntimeError("Resolve returned no item handle")


class AnonymousTimelineItemStub:
    pass


class TimelineItemDupStub:
    """Minimal timeline clip: source endFrame is an exclusive append boundary."""

    def __init__(self, mpi=None, unique_id="timeline-item-source"):
        self._mpi = mpi or object()
        self.unique_id = unique_id

    def GetMediaPoolItem(self):
        return self._mpi

    def GetStart(self):
        return 100

    def GetEnd(self):
        return 160

    def GetDuration(self):
        return 60

    def GetLeftOffset(self):
        return 50

    def GetUniqueId(self):
        return self.unique_id


class TimelineItemDupSourceStartStub(TimelineItemDupStub):
    def GetSourceStartFrame(self):
        return 72


class TimelineItemDupNoPoolStub(TimelineItemDupStub):
    def GetMediaPoolItem(self):
        return None


class MediaPoolItemWithIdStub:
    def __init__(self, unique_id):
        self.unique_id = unique_id

    def GetUniqueId(self):
        return self.unique_id


class AppendedTimelineItemStub(TimelineItemStub):
    def __init__(self, mpi, unique_id="timeline-item-new", name="duplicate.mov", start=105, end=165):
        super().__init__(unique_id=unique_id, name=name)
        self._mpi = mpi
        self.start = start
        self.end = end

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetDuration(self):
        return self.end - self.start

    def GetMediaPoolItem(self):
        return self._mpi


class TimelineWithTrackStub:
    def __init__(self, items):
        self.items = items

    def GetItemListInTrack(self, track_type, track_index):
        if track_type == "video" and track_index == 2:
            return self.items
        return []


class AppendClipInfosResultHandlingTest(unittest.TestCase):
    def test_serialize_appended_timeline_item_requires_item_handle(self):
        item_out, item_err = _serialize_appended_timeline_item(None, 0)

        self.assertIsNone(item_out)
        self.assertEqual(
            item_err,
            {"error": "Failed to append clip_infos to timeline: missing timeline item at index 0"},
        )

    def test_serialize_appended_timeline_item_requires_unique_id(self):
        item_out, item_err = _serialize_appended_timeline_item(TimelineItemStub(unique_id=""), 2)

        self.assertIsNone(item_out)
        self.assertEqual(
            item_err,
            {"error": "Failed to append clip_infos to timeline: missing timeline item id at index 2"},
        )

    def test_serialize_appended_timeline_item_rejects_invalid_item_handle(self):
        item_out, item_err = _serialize_appended_timeline_item(BrokenTimelineItemStub(), 1)

        self.assertIsNone(item_out)
        self.assertEqual(
            item_err,
            {"error": "Failed to append clip_infos to timeline: invalid timeline item at index 1"},
        )

    def test_serialize_appended_timeline_item_allows_empty_id_when_requested(self):
        item_out, item_err = _serialize_appended_timeline_item(
            TimelineItemStub(unique_id=""), 0, allow_empty_timeline_item_id=True
        )
        self.assertIsNone(item_err)
        self.assertEqual(
            item_out,
            {"timeline_item_id": None, "name": "synthetic_append_clip_infos.mp4"},
        )

    def test_serialize_appended_timeline_item_allows_missing_methods_when_requested(self):
        item_out, item_err = _serialize_appended_timeline_item(
            AnonymousTimelineItemStub(), 0, allow_empty_timeline_item_id=True
        )
        self.assertIsNone(item_err)
        self.assertEqual(item_out, {"timeline_item_id": None, "name": None})

    def test_serialize_appended_timeline_item_returns_summary(self):
        item_out, item_err = _serialize_appended_timeline_item(TimelineItemStub(), 0)

        self.assertIsNone(item_err)
        self.assertEqual(
            item_out,
            {
                "timeline_item_id": "timeline-item-123",
                "name": "synthetic_append_clip_infos.mp4",
            },
        )

    def test_append_clip_info_from_timeline_item_maps_trim_and_record(self):
        mpi = object()
        info, err = _append_clip_info_from_timeline_item(TimelineItemDupStub(mpi), target_track_index=2, record_frame_offset=5)
        self.assertIsNone(err)
        self.assertIs(info["mediaPoolItem"], mpi)
        self.assertEqual(info["startFrame"], 50)
        self.assertEqual(info["endFrame"], 110)
        self.assertEqual(info["recordFrame"], 105)
        self.assertEqual(info["trackIndex"], 2)
        self.assertEqual(info["mediaType"], 1)

    def test_append_clip_info_from_timeline_item_prefers_source_start_frame(self):
        info, err = _append_clip_info_from_timeline_item(
            TimelineItemDupSourceStartStub(), target_track_index=2, record_frame_offset=5
        )
        self.assertIsNone(err)
        self.assertEqual(info["startFrame"], 72)
        self.assertEqual(info["endFrame"], 132)

    def test_append_clip_info_from_timeline_item_rejects_no_media_pool(self):
        info, err = _append_clip_info_from_timeline_item(TimelineItemDupNoPoolStub(), 1, 0)
        self.assertIsNone(info)
        self.assertIn("error", err)

    def test_find_appended_timeline_item_summary_recovers_id_from_target_track(self):
        mpi = MediaPoolItemWithIdStub("media-1")
        original = AppendedTimelineItemStub(mpi, unique_id="timeline-item-source")
        appended = AppendedTimelineItemStub(mpi, unique_id="timeline-item-new")
        summary = _find_appended_timeline_item_summary(
            TimelineWithTrackStub([original, appended]),
            target_track_index=2,
            record_frame=105,
            duration=60,
            source_media_pool_item=mpi,
            source_timeline_item_id="timeline-item-source",
        )
        self.assertEqual(summary, {"timeline_item_id": "timeline-item-new", "name": "duplicate.mov"})


if __name__ == "__main__":
    unittest.main()
