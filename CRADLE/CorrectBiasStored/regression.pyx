# cython: language_level=3

import h5py
import numpy as np
import statsmodels.api as sm
import pyBigWig

cdef int COEF_LEN = 7

# The covariate values stored in the HDF files start at index 0 (0-index, obviously)
# The lowest start point for an analysis region is 3 (1-indexed), so we need to subtract
# 3 from the analysis start and end points to match them up with correct covariate values
# in the HDF files.
cdef int COVARIATE_FILE_INDEX_OFFSET = 3

cpdef performRegression(trainingSet, covariates, ctrlBWNames, ctrlScaler, experiBWNames, experiScaler, scatterplotSamples):
	xColumnCount = covariates.num + 1

	#### Get X matrix
	xView = np.ones((trainingSet.cumulativeRegionSize, xColumnCount), dtype=np.float64)

	cdef int currentRow = 0

	for trainingRegion in trainingSet:
		covariateFileName = covariates.covariateFileName(trainingRegion.chromo)
		with h5py.File(covariateFileName, "r") as covariateValues:
			non_selected_rows = np.where(np.isnan(covariates.selected))
			temp = covariateValues['covari'][trainingRegion.start - COVARIATE_FILE_INDEX_OFFSET:trainingRegion.end - COVARIATE_FILE_INDEX_OFFSET]
			temp = np.delete(temp, non_selected_rows, 1)
			xView[currentRow:currentRow + len(trainingRegion), 1:xColumnCount] = temp
			currentRow += len(trainingRegion)
	#### END Get X matrix

	#### Initialize COEF arrays
	COEFCTRL = np.zeros((len(ctrlBWNames), COEF_LEN), dtype=np.float64)
	COEFEXPR = np.zeros((len(experiBWNames), COEF_LEN), dtype=np.float64)

	ctrlPlotValues = {}
	experiPlotValues = {}

	for i, bwFileName in enumerate(ctrlBWNames):
		rawReadCounts = readCountData(bwFileName, trainingSet)
		readCounts = getReadCounts(rawReadCounts, trainingSet.cumulativeRegionSize, ctrlScaler[i])
		model = buildModel(readCounts, xView)

		COEFCTRL[i, :] = getCoefs(model.params, covariates.selected)

		ctrlPlotValues[bwFileName] = (readCounts[scatterplotSamples], model.fittedvalues[scatterplotSamples])

	for i, bwFileName in enumerate(experiBWNames):
		rawReadCounts = readCountData(bwFileName, trainingSet)
		readCounts = getReadCounts(rawReadCounts, trainingSet.cumulativeRegionSize, experiScaler[i])
		model = buildModel(readCounts, xView)

		COEFEXPR[i, :] = getCoefs(model.params, covariates.selected)

		experiPlotValues[bwFileName] = (readCounts[scatterplotSamples], model.fittedvalues[scatterplotSamples])

	return COEFCTRL, COEFEXPR, ctrlPlotValues, experiPlotValues

def readCountData(bwFileName, trainingSet):
	with pyBigWig.open(bwFileName) as bwFile:
		if pyBigWig.numpy == 1:
			for trainingRegion in trainingSet:
				regionReadCounts = bwFile.values(trainingRegion.chromo, trainingRegion.start, trainingRegion.end, numpy=True)
				yield regionReadCounts, len(trainingRegion)
		else:
			for trainingRegion in trainingSet:
				regionReadCounts = np.array(
					bwFile.values(trainingRegion.chromo, trainingRegion.start, trainingRegion.end)
				)
				yield regionReadCounts, len(trainingRegion)

cpdef getReadCounts(rawReadCounts, rowCount, scaler):
	cdef double [:] readCountsView
	cdef int ptr
	cdef int posIdx

	readCounts = np.zeros(rowCount, dtype=np.float64)
	readCountsView = readCounts

	ptr = 0
	for regionReadCounts, regionLength in rawReadCounts:
		regionReadCounts[np.isnan(regionReadCounts)] = 0.0
		regionReadCounts = regionReadCounts / scaler

		posIdx = 0
		while posIdx < regionLength:
			readCountsView[ptr + posIdx] = regionReadCounts[posIdx]
			posIdx += 1

		ptr += regionLength

	return readCounts

cpdef buildModel(readCounts, xView):
	#### do regression
	return sm.GLM(np.array(readCounts).astype(int), np.array(xView), family=sm.families.Poisson(link=sm.genmod.families.links.log)).fit()

cpdef getCoefs(modelParams, selectedCovariates):
	coef = np.zeros(COEF_LEN, dtype=np.float64)

	coef[0] = modelParams[0]

	paramIdx = 1
	for j in range(1, COEF_LEN):
		if np.isnan(selectedCovariates[j - 1]):
			coef[j] = np.nan
		else:
			coef[j] = modelParams[paramIdx]
			paramIdx += 1

	return coef
