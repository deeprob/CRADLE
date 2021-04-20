import os
import tempfile
import warnings
import h5py
import numpy as np
import statsmodels.api as sm
import py2bit
import pyBigWig

from CRADLE.correctbiasutils import TrainingRegion, TrainingSet
from CRADLE.correctbiasutils.cython import writeBedFile, plot

COEF_LEN = 7

cpdef performRegression(trainingSet, scatterplotSamples, covariates, ctrlBWNames, ctrlScaler, experiBWNames, experiScaler, outputDir, outputLabel):
	xColumnCount = covariates.num + 1

	#### Get X matrix
	xView = np.ones((trainingSet.xRowCount, xColumnCount), dtype=np.float64)

	cdef int currentRow = 0

	for trainingRegion in trainingSet:
		region_length = trainingRegion.analysisEnd - trainingRegion.analysisStart
		hdfFileName = covariates.hdfFileName(trainingRegion.chromo)
		with h5py.File(hdfFileName, "r") as hdfFile:
			non_selected_rows = np.where(np.isnan(covariates.selected))
			temp = hdfFile['covari'][trainingRegion.analysisStart - 3:trainingRegion.analysisEnd - 3]
			temp = np.delete(temp, non_selected_rows, 1)
			xView[currentRow:currentRow + region_length, 1:xColumnCount] = temp
			currentRow += region_length
	#### END Get X matrix

	#### Initialize COEF arrays
	COEFCTRL = np.zeros((len(ctrlBWNames), COEF_LEN), dtype=np.float64)
	COEFEXPR = np.zeros((len(experiBWNames), COEF_LEN), dtype=np.float64)

	for i, bwFileName in enumerate(ctrlBWNames):
		readCounts = getReadCounts(bwFileName, trainingSet, ctrlScaler[i])
		model = buildModel(readCounts, xView)

		COEFCTRL[i, :] = getCoefs(model, covariates)

		figName = figureFileName(outputDir, bwFileName, outputLabel)
		plot(readCounts, model.fittedvalues, figName, scatterplotSamples)

	for i, bwFileName in enumerate(experiBWNames):
		readCounts = getReadCounts(bwFileName, trainingSet, experiScaler[i])
		model = buildModel(readCounts, xView)

		COEFEXPR[i, :] = getCoefs(model, covariates)

		figName = figureFileName(outputDir, bwFileName, outputLabel)
		plot(readCounts, model.fittedvalues, figName, scatterplotSamples)

	return COEFCTRL, COEFEXPR

cpdef figureFileName(outputDir, bwFileName, outputLabel):
	bwName = '.'.join(bwFileName.rsplit('/', 1)[-1].split(".")[:-1])
	return os.path.join(outputDir, f"fit_{bwName}_{outputLabel}.png")

cpdef getReadCounts(bwFileName, trainingSet, scaler):
	cdef double [:] readCounts = np.zeros(trainingSet.xRowCount, dtype=np.float64)
	cdef int ptr
	cdef int posIdx

	with pyBigWig.open(bwFileName) as bwFile:
		ptr = 0
		for trainingRegion in trainingSet:
			regionReadCounts = np.array(
				bwFile.values(trainingRegion.chromo, trainingRegion.analysisStart, trainingRegion.analysisEnd)
			)
			regionReadCounts[np.isnan(regionReadCounts)] = 0.0
			regionReadCounts = regionReadCounts / scaler

			numPos = trainingRegion.analysisEnd - trainingRegion.analysisStart
			posIdx = 0
			while posIdx < numPos:
				readCounts[ptr + posIdx] = regionReadCounts[posIdx]
				posIdx += 1

			ptr += numPos

	return readCounts

cpdef buildModel(readCounts, xView):
	#### do regression
	return sm.GLM(np.array(readCounts).astype(int), np.array(xView), family=sm.families.Poisson(link=sm.genmod.families.links.log)).fit()

cpdef getCoefs(model, covariates):
	coef = np.zeros(COEF_LEN, dtype=np.float64)

	coef[0] = model.params[0]

	paramIdx = 1
	for j in range(1, COEF_LEN):
		if np.isnan(covariates.selected[j - 1]) == True:
			coef[j] = np.nan
		else:
			coef[j] = model.params[paramIdx]
			paramIdx += 1

	return coef


cpdef correctReadCount(taskArgs, covariates, faFileName, ctrlBWNames, ctrlScaler, COEFCTRL, COEFCTRL_HIGHRC, experiBWNames, experiScaler, COEFEXP, COEFEXP_HIGHRC, highRC, minFragFilterValue, binsize, outputDir):
	chromo = taskArgs[0]
	analysisStart = int(taskArgs[1])  # Genomic coordinates(starts from 1)
	analysisEnd = int(taskArgs[2])

	with py2bit.open(faFileName) as faFile:
		chromoEnd = int(faFile.chroms(chromo))

	###### GENERATE A RESULT MATRIX
	fragStart = analysisStart - covariates.fragLen + 1
	fragEnd = analysisEnd + covariates.fragLen - 1
	shearStart = fragStart - 2
	shearEnd = fragEnd + 2

	if shearStart < 1:
		shearStart = 1
		fragStart = 3
		analysisStart = max(analysisStart, fragStart)
	if shearEnd > chromoEnd:
		shearEnd = chromoEnd
		fragEnd = shearEnd - 2
		analysisEnd = min(analysisEnd, fragEnd)

	###### GET POSITIONS WHERE THE NUMBER OF FRAGMENTS > MIN_FRAGNUM_FILTER_VALUE
	selectedIdx, highReadCountIdx, starts = selectIdx(chromo, analysisStart, analysisEnd, ctrlBWNames, experiBWNames, highRC, minFragFilterValue)

	## OUTPUT FILES
	subfinalCtrlNames = [None] * len(ctrlBWNames)
	subfinalExperiNames = [None] * len(experiBWNames)

	if len(selectedIdx) == 0:
		return [subfinalCtrlNames, subfinalExperiNames, chromo]

	hdfFileName = covariates.hdfFileName(chromo)
	with h5py.File(hdfFileName, "r") as hdfFile:
		for rep, bwName in enumerate(ctrlBWNames):
			with pyBigWig.open(bwName) as bwFile:
				rcArr = np.array(bwFile.values(chromo, analysisStart, analysisEnd))
				rcArr[np.isnan(rcArr)] = 0.0
				rcArr = rcArr / ctrlScaler[rep]

			prdvals = np.exp(
				np.nansum(
					(hdfFile['covari'][(analysisStart-3):(analysisEnd-3)] * covariates.selected) * COEFCTRL[rep, 1:],
					axis=1
				) + COEFCTRL[rep, 0]
			)
			prdvals[highReadCountIdx] = np.exp(
				np.nansum(
					(hdfFile['covari'][(analysisStart-3):(analysisEnd-3)][highReadCountIdx] * covariates.selected) * COEFCTRL_HIGHRC[rep, 1:],
					axis=1
				) + COEFCTRL_HIGHRC[rep, 0]
			)

			rcArr = rcArr - prdvals
			rcArr = rcArr[selectedIdx]

			idx = np.where( (rcArr < np.finfo(np.float32).min) | (rcArr > np.finfo(np.float32).max))
			tempStarts = np.delete(starts, idx)
			rcArr = np.delete(rcArr, idx)
			if len(rcArr) > 0:
				with tempfile.NamedTemporaryFile(mode="w+t", suffix=".bed", dir=outputDir, delete=False) as subfinalCtrlFile:
					subfinalCtrlNames[rep] = subfinalCtrlFile.name
					writeBedFile(subfinalCtrlFile, tempStarts, rcArr, analysisEnd, binsize)

		for rep, bwName in enumerate(experiBWNames):
			with pyBigWig.open(bwName) as bwFile:
				rcArr = np.array(bwFile.values(chromo, analysisStart, analysisEnd))
				rcArr[np.isnan(rcArr)] = 0.0
				rcArr = rcArr / experiScaler[rep]

			prdvals = np.exp(
				np.nansum(
					(hdfFile['covari'][(analysisStart-3):(analysisEnd-3)] * covariates.selected) * COEFEXP[rep, 1:],
					axis=1
				) + COEFEXP[rep, 0]
			)
			prdvals[highReadCountIdx] = np.exp(
				np.nansum(
					(hdfFile['covari'][(analysisStart-3):(analysisEnd-3)][highReadCountIdx] * covariates.selected) * COEFEXP_HIGHRC[rep, 1:],
					axis=1
				) + COEFEXP_HIGHRC[rep, 0]
			)

			rcArr = rcArr - prdvals
			rcArr = rcArr[selectedIdx]

			idx = np.where( (rcArr < np.finfo(np.float32).min) | (rcArr > np.finfo(np.float32).max))
			tempStarts = np.delete(starts, idx)
			rcArr = np.delete(rcArr, idx)
			if len(rcArr) > 0:
				with tempfile.NamedTemporaryFile(mode="w+t", suffix=".bed", dir=outputDir, delete=False) as subfinalExperiFile:
					subfinalExperiNames[rep] = subfinalExperiFile.name
					writeBedFile(subfinalExperiFile, tempStarts, rcArr, analysisEnd, binsize)

	return [subfinalCtrlNames, subfinalExperiNames, chromo]

cpdef selectIdx(chromo, analysisStart, analysisEnd, ctrlBWNames, experiBWNames, highRC, minFragFilterValue):
	readCountSums = np.zeros(analysisEnd - analysisStart, dtype=np.float64)
	meanMinFragFilterValue = int(np.round(minFragFilterValue / (len(ctrlBWNames) + len(experiBWNames))))

	for rep, bwName in enumerate(ctrlBWNames):
		with pyBigWig.open(bwName) as bwFile:
			readCounts = np.array(bwFile.values(chromo, analysisStart, analysisEnd))
			readCounts[np.isnan(readCounts)] = 0.0

		if rep == 0:
			highReadCountIdx = np.where(readCounts > highRC)[0]
			overMeanReadCountIdx = np.where(readCounts >= meanMinFragFilterValue)[0]
		else:
			overMeanReadCountIdx_temp = np.where(readCounts >= meanMinFragFilterValue)[0]
			overMeanReadCountIdx = np.intersect1d(overMeanReadCountIdx, overMeanReadCountIdx_temp)

		readCountSums += readCounts

	for rep, bwName in enumerate(experiBWNames):
		with pyBigWig.open(bwName) as bwFile:
			readCounts = np.array(bwFile.values(chromo, analysisStart, analysisEnd))
			readCounts[np.isnan(readCounts)] = 0.0

		overMeanReadCountIdx_temp = np.where(readCounts >= meanMinFragFilterValue)[0]
		overMeanReadCountIdx = np.intersect1d(overMeanReadCountIdx, overMeanReadCountIdx_temp)

		readCountSums += readCounts
	del overMeanReadCountIdx_temp

	idx = np.where(readCountSums > minFragFilterValue)[0].tolist()
	idx = np.intersect1d(idx, overMeanReadCountIdx)

	highReadCountIdx = np.intersect1d(highReadCountIdx, idx)

	if len(idx) == 0:
		return np.array([]), np.array([]), np.array([])

	starts = np.array(list(range(analysisStart, analysisEnd)))[idx]

	return idx, highReadCountIdx, starts
