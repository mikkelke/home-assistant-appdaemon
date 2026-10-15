"""What to do for each maintenance task, step by step, for the sheet of its row on the dashboard's Now
timeline. The one place the text lives: the rows carry it as `steps` and `warn`, the dashboard only
shows it. Plain ASCII English, three to six steps and the safety note the manual stresses.

The Miele guides follow the operating instructions of the machines (Danish, the PDFs the distilled
docs in apps/appliances/docs cite): the dishwasher's Rengoring og vedligeholdelse pp. 63-69, the
washer's pp. 15 and 37-41, the dryer's pp. 11 and 40-45. The Rober2 guides are the Roborock Q8 Max
routine for each part.
"""

from typing import NamedTuple


class Guide(NamedTuple):
    steps: tuple
    warn: str


GUIDES = {
    "dishwasher:hygiene": Guide(
        steps=(
            "Pick the Intensive 75 C programme on the dishwasher and start it.",
            "It cleans the wash cabinet and stops germs and odour. Miele says to run it about once a month.",
            "Pick Intensive on the dishwasher card in the dashboard, during the run or after it, so the run is logged.",
        ),
        warn="The water is hot while it runs. Open the door with care.",
    ),
    "dishwasher:filters": Guide(
        steps=(
            "Switch the dishwasher off, take out the lower basket and lift off the lower spray arm (turn its collar "
            "anticlockwise).",
            "Pick out coarse food bits, then lift out the filter combination and pull the micro filter out of it.",
            "Rinse the filters under running water. Use a dish brush if needed.",
            "Middle arm: turn its collar clockwise. Upper arm: push it up and unscrew it anticlockwise (take the "
            "cutlery tray out first, if fitted).",
            "Poke food out of the arm nozzles with a pointed object, then rinse all the arms.",
            "Refit in reverse. Turn the lower arm's collar clockwise until its arrow meets the lock symbol. Check the "
            "arms spin freely.",
        ),
        warn="Never run the dishwasher without the filters and the lower spray arm.",
    ),
    "dishwasher:check": Guide(
        steps=(
            "Wipe the door seal with a damp cloth to remove food bits. The spray does not reach it, so mould can grow "
            "there.",
            "Wipe spilt food and drink off the sides of the door.",
            "Wipe the control panel with a damp cloth only.",
            "Clean the front with a clean sponge cloth, hand dish soap and warm water. Dry it with a soft cloth.",
            "Look the whole machine over for wear, damage or leaks.",
        ),
        warn="No scouring pads or powder, ammonia, acid, chloride, descaler, oven or glass cleaner, steel wool or "
             "steam cleaner: they damage the surfaces.",
    ),
    "washer:drum": Guide(
        steps=(
            "Pull the plug. Pull the drawer out to the stop, press the lock button down and lift it out.",
            "Wash the drawer and the fabric softener channel in warm water, with a brush for the channel.",
            "Pull the siphon out of its chamber, rinse it and the pipe it sits on, and put it back.",
            "Scrub soap and limescale off the rinse nozzles in the drawer slot with a bottle brush. Put the drawer back.",
            "Plug in. Start Bomuld 90 C with an empty drum and a little universal powder detergent (or Miele machine "
            "cleaner).",
            "Pick Drum clean on the washer card in the dashboard so the clean is logged. Afterwards leave the door and "
            "the drawer ajar to dry.",
        ),
        warn="The washer has mains voltage even when it is switched off. Pull the plug before you clean the drawer.",
    ),
    "washer:inlet": Guide(
        steps=(
            "Close the water tap and unscrew the inlet hose from it.",
            "Hose end: pull the rubber seal out, grab the plastic filter with pliers, pull it out and rinse it clean.",
            "Machine end: carefully unscrew the fluted plastic nut with pliers, pull out the second filter and rinse it.",
            "Refit both filters in reverse order. They must go back after cleaning.",
            "Screw the hose back onto the tap and open the tap.",
            "Check for drips at the connection. Tighten it more if water seeps out.",
        ),
        warn="Close the water tap and pull the plug before you start.",
    ),
    "dryer:plinth": Guide(
        steps=(
            "Open the plinth flap: press the round recess on it and it pops up.",
            "Take the handle and pull the plinth filter out. Pull the handle off the filter.",
            "Wash the filter under running water, pressing the water out now and then, until no residue shows.",
            "Wipe lint off the handle and the flap with a damp cloth. Do not damage the rubber seal.",
            "Drying slower? Also vacuum the opening above the door lint filters (long nozzle) and rinse the filters in "
            "warm water.",
            "Dry everything well. Push the plinth filter fully in and close the flap. Open and close the door while "
            "the dryer is on to reset the filter lamp.",
        ),
        warn="Unplug the dryer only after its cool-down is finished. Never run it without the plinth filter or with "
             "the flap open. Wet filters cause faults.",
    ),
    "rober2:filter": Guide(
        steps=(
            "Switch the robot off and take out the dust bin.",
            "Pull the old filter out of the bin and put the new one in.",
            "Refit the dust bin.",
            "Reset the filter counter in the Roborock app.",
        ),
        warn="",
    ),
    "rober2:main_brush": Guide(
        steps=(
            "Switch the robot off and turn it upside down on a soft surface.",
            "Press the clips and lift off the main brush cover, then pull out the old brush.",
            "Fit the new brush and snap the cover back on.",
            "Reset the main brush counter in the Roborock app.",
        ),
        warn="",
    ),
    "rober2:side_brush": Guide(
        steps=(
            "Switch the robot off and turn it upside down on a soft surface.",
            "Pull the old side brush off (unscrew it if it is screwed on).",
            "Press the new brush on until it sits firm.",
            "Reset the side brush counter in the Roborock app.",
        ),
        warn="",
    ),
    "rober2:sensor": Guide(
        steps=(
            "Switch the robot off and take it off the dock.",
            "Wipe the cliff sensors on the underside and the wall sensor on the bumper with a soft dry cloth.",
            "Wipe the charging contacts on the robot and on the dock.",
            "Reset the sensor counter in the Roborock app.",
        ),
        warn="",
    ),
}
