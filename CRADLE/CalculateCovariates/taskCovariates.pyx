# cython: language_level=3

import tempfile
import h5py
import numpy as np
import py2bit
import pyBigWig

import CRADLE.CalculateCovariates.covariateUtils as cu

from CRADLE.CalculateCovariates import vari
from CRADLE.correctbiasutils import vari as commonVari



cpdef calculateBoundaries(chromoEnd, analysisStart, analysisEnd, binStart, binEnd, nBins):
	fragStart = binStart + 1 - vari.FRAGLEN
	fragEnd = binEnd + vari.FRAGLEN  # not included
	shearStart = fragStart - 2
	shearEnd = fragEnd + 2 # not included

	if shearStart < 1:
		shearStart = 1
		fragStart = 3
		binStart = max(binStart, fragStart)
		analysisStart = max(analysisStart, fragStart)

		###### EDIT BINSTART/ BINEND
		nBins = (analysisEnd - analysisStart) // vari.BINSIZE
		leftValue = (analysisEnd - analysisStart) % vari.BINSIZE
		if leftValue != 0:
			nBins += 1
			binEnd = (analysisStart + (nBins-1) * vari.BINSIZE + analysisEnd) // 2
		else:
			binEnd = binStart + (nBins-1) * vari.BINSIZE

		fragEnd = binEnd + vari.FRAGLEN
		shearEnd = fragEnd + 2

	if shearEnd > chromoEnd:
		analysisEndModified = min(analysisEnd, chromoEnd - 2)  # not included

		if analysisEndModified == analysisEnd:
			shearEnd = chromoEnd
			fragEnd = shearEnd - 2
		else:
			analysisEnd = analysisEndModified

			###### EDIT BINSTART/ BINEND
			nBins = (analysisEnd - analysisStart) // vari.BINSIZE
			leftValue = (analysisEnd - analysisStart) % vari.BINSIZE
			if leftValue != 0:
				nBins += 1
				binEnd = (analysisStart + (nBins-1) * vari.BINSIZE + analysisEnd) // 2
			else:
				binEnd = binStart + (nBins-1) * vari.BINSIZE

			fragEnd = binEnd + vari.FRAGLEN
			shearEnd = fragEnd + 2

			if shearEnd > chromoEnd:
				shearEnd = chromoEnd
				fragEnd = shearEnd - 2

	return fragStart, fragEnd, shearStart, shearEnd, binStart, binEnd, nBins


cpdef mapValues(chromo, fragStart, fragEnd):
	mapFile = pyBigWig.open(vari.MAPFILE)
	mapValue = np.array(mapFile.values(chromo, fragStart, fragEnd))

	mapValue[np.where(mapValue == 0)] = np.nan
	mapValue = np.log(mapValue)
	mapValue[np.where(np.isnan(mapValue))] = -6.0

	mapValueView = cu.memoryView(mapValue)
	mapFile.close()

	return mapValueView


cpdef gquadValues(chromo, fragStart, fragEnd):
	gquadFile = [0] * len(vari.GQAUDFILE)
	gquadValue = [0] * len(vari.GQAUDFILE)

	for i in range(len(vari.GQAUDFILE)):
		gquadFile[i] = pyBigWig.open(vari.GQAUDFILE[i])
		gquadValue[i] = gquadFile[i].values(chromo, fragStart, fragEnd)
		gquadFile[i].close()

	gquadValue = np.array(gquadValue)
	gquadValue = np.nanmax(gquadValue, axis=0)
	gquadValue[np.where(gquadValue == 0)] = np.nan
	gquadValue = np.log(gquadValue / float(vari.GQAUD_MAX))

	gquadValue[np.where(np.isnan(gquadValue))] = float(-5)
	gquadValueView = cu.memoryView(gquadValue)

	return gquadValueView


cpdef fragCovariates(idx, pastMer1, pastMer2, pastStartGibbs, sequence, mapValues, gquadValues):
	covariates = np.zeros(vari.COVARI_NUM)
	covariIdxPtr = 0

	if vari.SHEAR == 1:
		###  mer1
		mer1 = sequence[(idx-2):(idx+3)].upper()
		if 'N' in mer1:
			pastMer1 = -1
			mgwIdx = vari.N_MGW
			protIdx = vari.N_PROT
		else:
			if pastMer1 == -1: # there is no information on pastMer1
				pastMer1, mgwIdx, protIdx = cu.find5merProb(mer1)
			else:
				pastMer1, mgwIdx, protIdx = cu.edit5merProb(pastMer1, mer1[0], mer1[4])

		###  mer2
		fragEndIdx = idx + vari.FRAGLEN
		mer2 = sequence[(fragEndIdx-3):(fragEndIdx+2)].upper()
		if 'N' in mer2:
			pastMer2 = -1
			mgwIdx = mgwIdx + vari.N_MGW
			protIdx = protIdx + vari.N_PROT
		else:
			if pastMer2 == -1:
				pastMer2, add1, add2 = cu.findComple5merProb(mer2)
			else:
				pastMer2, add1, add2 = cu.editComple5merProb(pastMer2, mer2[0], mer2[4])
			mgwIdx = mgwIdx + add1
			protIdx = protIdx + add2

		covariates[covariIdxPtr] = mgwIdx
		covariates[covariIdxPtr+1] = protIdx
		covariIdxPtr += 2

	if vari.PCR == 1:
		sequenceIdx = sequence[idx:(idx+vari.FRAGLEN)]
		if pastStartGibbs == -1:
			startGibbs, gibbs = cu.findStartGibbs(sequenceIdx, vari.FRAGLEN)
		else:
			oldDimer = sequenceIdx[0:2].upper()
			newDimer = sequenceIdx[(vari.FRAGLEN-2):vari.FRAGLEN].upper()
			startGibbs, gibbs = cu.editStartGibbs(oldDimer, newDimer, pastStartGibbs)

		annealIdx, denatureIdx = cu.convertGibbs(gibbs)

		covariates[covariIdxPtr] = annealIdx
		covariates[covariIdxPtr+1] = denatureIdx
		covariIdxPtr += 2

	if vari.MAP == 1:
		map1 = mapValues[(idx-2)]
		map2 = mapValues[(idx+vari.FRAGLEN-2-vari.KMER)]
		mapIdx = map1 + map2

		covariates[covariIdxPtr] = mapIdx
		covariIdxPtr += 1

	if vari.GQUAD == 1:
		covariates[covariIdxPtr] = np.nanmax(np.asarray(gquadValues[(idx-2):(idx+vari.FRAGLEN-2)]))

	return covariates, pastMer1, pastMer2, pastStartGibbs


cpdef calculateContinuousFrag(sequence, mapValueView, gquadValueView, covariDataSet, result, analysisStart, shearStart, fragEnd, binStart, binEnd):
	##### INITIALIZE VARIABLES
	if vari.SHEAR == 1:
		pastMer1 = -1
		pastMer2 = -1
	if vari.PCR == 1:
		pastStartGibbs = -1

	resultStartIdx = -1
	resultEndIdx = -1

	##### INDEX IN 'sequence'
	startIdx = 2  # index in the genome sequence file (Included in the range)
	endIdx = (fragEnd - vari.FRAGLEN) - shearStart + 1   # index in the genome sequence file (Not included in the range)

	for idx in range(startIdx, endIdx):
		covariates, pastMer1, pastMer2, pastStartGibbs = fragCovariates(idx, pastMer1, pastMer2, pastStartGibbs, sequence, mapValueView, gquadValueView)

		### DETERMINE WHICH ROWS TO EDIT IN RESULT MATRIX
		thisFragStart = idx + shearStart
		thisFragEnd = thisFragStart + vari.FRAGLEN

		if resultStartIdx == -1:
			resultStartIdx = 0
			resultEndIdx = 1 # not included
			if not np.isnan(result[resultEndIdx, 0]):
				while result[resultEndIdx, 0] < thisFragEnd:
					resultEndIdx = resultEndIdx + 1
					if resultEndIdx > vari.FRAGLEN:
						resultEndIdx = resultEndIdx - (vari.FRAGLEN+1)
					if np.isnan(result[resultEndIdx, 0]):
						break
			maxBinPos = binStart + vari.FRAGLEN
			numPoppedPos = 0
		else:
			while result[resultStartIdx, 0] < thisFragStart:
				## pop the element
				line = []
				for covariPos in range(vari.COVARI_NUM):
					line.extend([ result[resultStartIdx, (covariPos+1)]  ])
					result[resultStartIdx, (covariPos+1)] = float(0)
				covariDataSet[analysisStart + numPoppedPos, :] = line

				numPoppedPos = numPoppedPos + 1
				if maxBinPos >= binEnd:
					result[resultStartIdx, 0] = np.nan
				else:
					result[resultStartIdx, 0] = maxBinPos + 1
					maxBinPos = maxBinPos + 1

				resultStartIdx = resultStartIdx + 1
				if resultStartIdx > vari.FRAGLEN:
					resultStartIdx = resultStartIdx - (vari.FRAGLEN+1)

			if not np.isnan(result[resultEndIdx, 0]):
				while result[resultEndIdx, 0] < thisFragEnd:
					resultEndIdx = resultEndIdx + 1
					if resultEndIdx > vari.FRAGLEN:
						resultEndIdx = resultEndIdx - (vari.FRAGLEN+1)
					if np.isnan(result[resultEndIdx, 0]):
						break

		if resultEndIdx < resultStartIdx:
			for pos in range(resultStartIdx, (vari.FRAGLEN+1)):
				for covariPos in range(vari.COVARI_NUM):
					result[pos, covariPos+1] = result[pos, covariPos+1] + covariates[covariPos]
			for pos in range(0, resultEndIdx):
				for covariPos in range(vari.COVARI_NUM):
					result[pos, covariPos+1] = result[pos, covariPos+1] + covariates[covariPos]
		else:
			for pos in range(resultStartIdx, resultEndIdx):
				for covariPos in range(vari.COVARI_NUM):
					result[pos, covariPos+1] = result[pos, covariPos+1] + covariates[covariPos]

		if idx == (endIdx-1): # the last fragment
			### pop the rest of positions that are not np.nan
			if resultEndIdx < resultStartIdx:
				for pos in range(resultStartIdx, (vari.FRAGLEN+1)):
					line = []
					for covariPos in range(vari.COVARI_NUM):
						line.extend([ result[pos, (covariPos+1)]  ])
					covariDataSet[analysisStart + numPoppedPos, :] = line

					numPoppedPos = numPoppedPos + 1

				for pos in range(0, resultEndIdx):
					line = []
					for covariPos in range(vari.COVARI_NUM):
						line.extend([ result[pos, (covariPos+1)]  ])
					covariDataSet[analysisStart + numPoppedPos, :] = line

					numPoppedPos = numPoppedPos + 1
			else:
				for pos in range(resultStartIdx, resultEndIdx):
					line = []
					for covariPos in range(vari.COVARI_NUM):
						line.extend([ result[pos, (covariPos+1)]  ])
					covariDataSet[analysisStart + numPoppedPos, :] = line

					numPoppedPos = numPoppedPos + 1


cpdef calculateDiscreteFrag(chromoEnd, sequence, mapValueView, gquadValueView, covariDataSet, analysisStart, shearStart, binStart, binEnd, nBins):
	for resultIdx in range(nBins): # for each bin
		if resultIdx == (nBins-1):
			pos = binEnd
		else:
			pos = binStart + resultIdx * vari.BINSIZE

		thisBinFirstFragStart = pos + 1 - vari.FRAGLEN
		thisBinLastFragStart = pos

		if thisBinFirstFragStart < 3:
			thisBinFirstFragStart = 3
		if (thisBinLastFragStart + vari.FRAGLEN) > (chromoEnd - 2):
			thisBinLastFragStart = chromoEnd - 2 - vari.FRAGLEN

		thisBinNumFrag = thisBinLastFragStart - thisBinFirstFragStart + 1

		thisBinFirstFragStartIdx = thisBinFirstFragStart - shearStart

		##### INITIALIZE VARIABLES
		if vari.SHEAR == 1:
			pastMer1 = -1
			pastMer2 = -1
		if vari.PCR == 1:
			pastStartGibbs = -1

		line = np.zeros(vari.COVARI_NUM)
		for binFragIdx in range(thisBinNumFrag):
			idx = thisBinFirstFragStartIdx + binFragIdx
			covariates, pastMer1, pastMer2, pastStartGibbs = fragCovariates(idx, pastMer1, pastMer2, pastStartGibbs, sequence, mapValueView, gquadValueView)

			line += covariates

		covariDataSet[analysisStart + resultIdx, :] = line


cpdef calculateTaskCovariates(chromo, outputFilename, regions):
	with py2bit.open(vari.GENOME) as genome:
		chromoEnd = int(genome.chroms(chromo))

	##### CREATE COVARIATE FILE
	f = h5py.File(outputFilename, "w")
	covariDataSet = f.create_dataset("covari", (chromoEnd, vari.COVARI_NUM), dtype='f', compression="gzip")

	##### CALCULATE COVARIATE VALUES
	for _chromo, analysisStart, analysisEnd in regions:
		continuousFrag = False

		#### DECIDE IF 'calculateContinuousFrag' or 'calculateDiscreteFrag'
		#### TODO: What is the logic here? why do these things determine continuousFrag for discreteFrag?
		if (analysisStart + vari.BINSIZE) >= analysisEnd:
			firstBinPos = (analysisStart + analysisEnd) // 2
			lastBinPos = firstBinPos
			nBins = 1
			continuousFrag = True
		else:
			firstBinPos = (2*analysisStart + vari.BINSIZE) // 2
			if (analysisStart + 2*vari.BINSIZE) > analysisEnd:
				secondBinPos = (analysisStart + vari.BINSIZE + analysisEnd) // 2
				lastBinPos = secondBinPos
				nBins = 2
			else:
				secondBinPos = (2*analysisStart + 3*vari.BINSIZE) // 2
				leftValue = (analysisEnd - analysisStart) % vari.BINSIZE
				nBins = (analysisEnd - analysisStart) // vari.BINSIZE
				if leftValue == 0:
					lastBinPos = firstBinPos + (nBins-1) * vari.BINSIZE ## should be included in the analysis
				else:
					nBins += 1
					lastBinPos = (analysisStart + (nBins-1) * vari.BINSIZE + analysisEnd) // 2  ## should be included in the analysis

			if secondBinPos - firstBinPos <= vari.FRAGLEN:
				continuousFrag = True

		###### CALCULATE INDEX VARIABLE
		fragStart, fragEnd, shearStart, shearEnd, binStart, binEnd, nBins = calculateBoundaries(chromoEnd, analysisStart, analysisEnd, firstBinPos, lastBinPos, nBins)

		###### GET SEQUENCE
		sequence = genome.sequence(chromo, (shearStart-1), (shearEnd-1))

		##### GET BIASES INFO FROM FILES
		if vari.MAP == 1:
			mapValueView = mapValues(chromo, fragStart, fragEnd)

		if vari.GQUAD == 1:
			gquadValueView = gquadValues(chromo, fragStart, fragEnd)

		if continuousFrag:
			###### GENERATE A RESULT MATRIX
			result = makeMatrixContinuousFrag(binStart, binEnd, nBins)

			calculateContinuousFrag(sequence, mapValueView, gquadValueView, covariDataSet, result, analysisStart, shearStart, fragEnd, binStart, binEnd)
		else:
			calculateDiscreteFrag(chromoEnd, sequence, mapValueView, gquadValueView, covariDataSet, analysisStart, shearStart, binStart, binEnd, nBins)

	f.close()


cpdef makeMatrixContinuousFrag(binStart, binEnd, nBins):
	result = np.zeros(((vari.FRAGLEN+1), (vari.COVARI_NUM+1)), dtype=np.float64)
	for i in range(vari.FRAGLEN+1):
		pos = binStart + i * vari.BINSIZE

		if pos > binEnd:
			result[i, 0] = np.nan
		else:
			result[i, 0] = pos

	if nBins == (vari.FRAGLEN+1):
		result[vari.FRAGLEN, 0] = binEnd

	return result
