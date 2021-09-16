import struct

import h5py # type: ignore
import numpy as np
import pyBigWig # type: ignore

from CRADLE.correctbiasutils import CORRECTED_RC_TEMP_FILE_STRUCT_FORMAT, SONICATION_SHEAR_BIAS_OFFSET, START_INDEX_ADJUSTMENT, outputCorrectedTmpFile
from CRADLE.correctbiasutils.cython import coalesceSections # type: ignore

# The covariate values stored in the HDF files start at index 0 (0-index, obviously)
# The lowest start point for an analysis region is 3 (1-indexed), so we need to subtract
# 3 from the analysis start and end points to match them up with correct covariate values
# in the HDF files.
COVARIATE_FILE_INDEX_OFFSET = 3

def alignCoordinatesToCovariateFileBoundaries(region, chromoEnds, fragLen):
	chromo, analysisStart, analysisEnd = region
	chromoEnd = chromoEnds[chromo]

	# Define a region of fragments of length fragLen
	fragRegionStart = analysisStart - fragLen + START_INDEX_ADJUSTMENT
	fragRegionEnd = analysisEnd + fragLen - START_INDEX_ADJUSTMENT

	# Define a region that includes base pairs used to model shearing/sonication bias
	shearStart = fragRegionStart - SONICATION_SHEAR_BIAS_OFFSET
	shearEnd = fragRegionEnd + SONICATION_SHEAR_BIAS_OFFSET

	# Make sure the analysisStart and analysisEnd fall within the boundaries of the region
	# covariates have been precomputed for
	if shearStart < 1:
		fragRegionStart = SONICATION_SHEAR_BIAS_OFFSET + START_INDEX_ADJUSTMENT
		analysisStart = max(analysisStart, fragRegionStart)

	if shearEnd > chromoEnd:
		fragRegionEnd = chromoEnd - SONICATION_SHEAR_BIAS_OFFSET
		analysisEnd = min(analysisEnd, fragRegionEnd)

	return (analysisStart, analysisEnd)

def writeCorrectedReads(outFile, sectionCount, starts, ends, values):
	for i in range(sectionCount):
		outFile.write(struct.pack(CORRECTED_RC_TEMP_FILE_STRUCT_FORMAT, starts[i], ends[i], values[i]))

def correctReadCount(regions, chromoEnds, covariates, trainingBWName, bwNames, scalers, COEFs, COEF_HIGHRCs, highRC, minFragFilterValue, binsize, outputDir):
	meanMinFragFilterValue = int(np.round(minFragFilterValue / len(bwNames)))

	for chromoRegionData in regions:
		chromo, chromoId, chromoRegions = chromoRegionData

		# Align regions to the covariate file boundaries, generate the "overall" indices used later and load up the training read counts
		# all in one loop
		overallIndices = []
		adjustedChromoRegions = []
		with pyBigWig.open(trainingBWName) as bwFile:
			trainingReadCounts = np.zeros(bwFile.chroms(chromo), dtype=np.float32)
			for region in chromoRegions:
				# Align the region to covariate file boundaries
				start, end = alignCoordinatesToCovariateFileBoundaries(region, chromoEnds, covariates.fragLen)
				adjustedChromoRegions.append((start, end))

				# generate the "overall" index of locations with total read counts > minFragFilterValue
				overallIndices.append(selectOverallIdx(chromo, start, end, bwNames, minFragFilterValue))

				# load the training read counts
				if pyBigWig.numpy == 1:
					trainingReadCounts[start:end] = bwFile.values(chromo, start, end, numpy=True)
				else:
					trainingReadCounts[start:end] = bwFile.values(chromo, start, end)

			trainingReadCounts[np.isnan(trainingReadCounts)] = 0.0

		del chromoRegions # We don't need this anymore and should break if we use it

		for bwName, scaler, COEF, COEF_HIGHRC in zip(bwNames, scalers, COEFs, COEF_HIGHRCs):
			bwFile = pyBigWig.open(bwName)
			correctedReadCountOutputFile = open(outputCorrectedTmpFile(outputDir, chromo, chromoId, bwName), "wb")
			covariateFile = h5py.File(covariates.covariateFileName(chromo), "r")
			covariateValues = covariateFile['covari']

			for region, overallIdx in zip(adjustedChromoRegions, overallIndices):
				analysisStart, analysisEnd = region

				###### GET POSITIONS WHERE THE NUMBER OF FRAGMENTS > MIN_FRAGNUM_FILTER_VALUE
				if pyBigWig.numpy == 1:
					rcArr = bwFile.values(chromo, analysisStart, analysisEnd, numpy=True).astype(np.float64)
				else:
					rcArr = np.array(bwFile.values(chromo, analysisStart, analysisEnd))
				rcArr[np.isnan(rcArr)] = 0.0
				readCountIdx = selectReplicateIdx(rcArr, overallIdx, meanMinFragFilterValue)

				if len(readCountIdx) == 0:
					# No locations in the region reach the read count threshold of meanMinFragFilterValue
					continue

				rcArr = rcArr / scaler

				## OUTPUT FILES
				values = covariateValues[(analysisStart - COVARIATE_FILE_INDEX_OFFSET):(analysisEnd - COVARIATE_FILE_INDEX_OFFSET)]
				values = values * covariates.selected
				prdvals = np.exp(
					np.nansum(values * COEF[1:], axis=1) + COEF[0]
				)

				highReadCountIdx = selectHighRCIdx(trainingReadCounts[analysisStart:analysisEnd], overallIdx, highRC)
				prdvals[highReadCountIdx] = np.exp(
					np.nansum(values[highReadCountIdx] * COEF_HIGHRC[1:], axis=1) + COEF_HIGHRC[0]
				)

				rcArr = rcArr - prdvals
				rcArr = rcArr[readCountIdx]
				starts = np.arange(analysisStart, analysisEnd)[readCountIdx]

				outOfRangeIdx = np.where((rcArr < np.finfo(np.float32).min) | (rcArr > np.finfo(np.float32).max))
				starts = np.delete(starts, outOfRangeIdx)
				rcArr = np.delete(rcArr, outOfRangeIdx)

				if len(rcArr) > 0:
					rcArr = np.rint(rcArr)
					coalescedSectionCount, startEntries, endEntries, valueEntries = coalesceSections(starts, rcArr, analysisEnd, binsize)
					writeCorrectedReads(correctedReadCountOutputFile, coalescedSectionCount, startEntries, endEntries, valueEntries)
			bwFile.close()
			covariateFile.close()
			correctedReadCountOutputFile.close()


def selectOverallIdx(chromo, analysisStart, analysisEnd, bwNames, minFragFilterValue):
	readCountSums = np.zeros(analysisEnd - analysisStart, dtype=np.float32)

	for bwName in bwNames:
		with pyBigWig.open(bwName) as bwFile:
			if pyBigWig.numpy == 1:
				readCounts = bwFile.values(chromo, analysisStart, analysisEnd, numpy=True)
			else:
				readCounts = np.array(bwFile.values(chromo, analysisStart, analysisEnd), dtype=np.float32)
			readCounts[np.isnan(readCounts)] = 0.0

		readCountSums += readCounts

	idx = np.where(readCountSums > minFragFilterValue)[0]

	return idx

def selectHighRCIdx(trainingReadCounts, idx, highRC):
	highReadCountIdx = np.where(trainingReadCounts > highRC)[0]
	highReadCountIdx = np.intersect1d(highReadCountIdx, idx)
	return highReadCountIdx

def selectReplicateIdx(readCounts, idx, meanMinFragFilterValue):
	replicateIdx = np.where(readCounts >= meanMinFragFilterValue)[0]
	replicateIdx = np.intersect1d(replicateIdx, idx)
	return replicateIdx
