#!/usr/bin/python

import os
import argparse
import logging

subfolders = ("Tutorials", "Examples", "Projects", "Libraries", "Articles",
        "Tools", "Notes")

parser = argparse.ArgumentParser()
parser.add_argument("folder",
        help="Main folder name for programming language or library")
parser.add_argument("-v", "--verbose", help="increase output verbosity",
        action="store_true")
args = parser.parse_args()

if args.verbose:
    logging.basicConfig(level=logging.DEBUG)

logger = logging.getLogger("GENERATOR")

main_folder = os.path.join(os.getcwd(), args.folder)
if not os.path.exists(main_folder):
    logger.debug('Creating %s folder.' % main_folder)
    os.makedirs(main_folder)
    print("%s created successfully!" % args.folder)
else:
    logger.debug('%s folder already exists!' % main_folder)

for sub in subfolders:
    logger.debug('sub directory %s handling...' % sub)
    sub_folder = os.path.join(main_folder, sub)
    logger.debug('determinated directory path is %s for %s' % (sub_folder, sub))
    if not os.path.exists(sub_folder):
        logger.debug('Creating %s folder.' % sub_folder)
        os.makedirs(sub_folder)
        print("%s created successfully!" % os.path.join(args.folder, sub))
    else:
        logger.debug('%s folder already exists!' % sub_folder)

