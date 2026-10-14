"""When Miele's monthly drum clean is due (docs/miele-wea035-washer.md: Bomuld 90 °C monthly).
Pure; the washer monitor's confirm push and HouseUpcoming's drum row both ask here, so they always
agree. The programme itself is washer_profiles.DRUM_CLEAN.
"""

from datetime import timedelta

INTERVAL = timedelta(days=30)


def due(last_clean, now):
    """True while a drum clean is due: none on record (`last_clean` None) or INTERVAL since the last
    one finished. Both times are aware datetimes."""
    return last_clean is None or now - last_clean >= INTERVAL
