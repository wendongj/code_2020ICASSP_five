# Copyright (c) 2019 Robin Scheibler
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
Overdetermined Blind Source Separation offline example
======================================================

This script requires the `mir_eval` to run, and `tkinter` and `sounddevice` packages for the GUI option.
"""
import sys
import time

import matplotlib
import numpy as np
from auxiva_pca import auxiva_pca
from five import five

# Get the data if needed
from get_data import get_data, samples_dir
from ive import ogive
from mir_eval.separation import bss_eval_sources
from overiva import overiva
from pyroomacoustics.bss import projection_back
from routines import PlaySoundGUI, grid_layout, random_layout, semi_circle_layout
from scipy.io import wavfile

get_data()
from samples.generate_samples import sampling, wav_read_center

# Once we are sure the data is there, import some methods
# to select and read samples
sys.path.append(samples_dir)


# We concatenate a few samples to make them long enough
if __name__ == "__main__":

    algo_choices = [
        "five",
        "overiva",
        "auxiva_pca",
        "ogive",
        "auxiva",
        "ilrma",
    ]
    model_choices = ["laplace", "gauss"]
    init_choices = ["eye", "eig"]

    import argparse

    parser = argparse.ArgumentParser(
        description="Demonstration of blind source extraction using FIVE."
    )
    parser.add_argument(
        "--no_cb", action="store_true", help="Removes callback function"
    )
    parser.add_argument("-b", "--block", type=int, default=2048, help="STFT block size")
    parser.add_argument(
        "-a",
        "--algo",
        type=str,
        default=algo_choices[0],
        choices=algo_choices,
        help="Chooses BSS method to run",
    )
    parser.add_argument(
        "-d",
        "--dist",
        type=str,
        default=model_choices[0],
        choices=model_choices,
        help="IVA model distribution",
    )
    parser.add_argument(
        "-i",
        "--init",
        type=str,
        default=init_choices[0],
        choices=init_choices,
        help="Initialization, eye: identity, eig: principal eigenvectors",
    )
    parser.add_argument("-m", "--mics", type=int, default=5, help="Number of mics")
    parser.add_argument("-s", "--srcs", type=int, default=2, help="Number of sources")
    parser.add_argument(
        "-n", "--n_iter", type=int, default=51, help="Number of iterations"
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Creates a small GUI for easy playback of the sound samples",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Saves the output of the separation to wav files",
    )
    args = parser.parse_args()

    assert args.srcs <= args.mics, "More sources than microphones is not supported"

    if args.gui:
        print("setting tkagg backend")
        # avoids a bug with tkinter and matplotlib
        import matplotlib

        matplotlib.use("TkAgg")

    import pyroomacoustics as pra

    # Simulation parameters
    fs = 16000
    absorption, max_order = 0.35, 17  # RT60 == 0.3
    # absorption, max_order = 0.45, 12  # RT60 == 0.2
    n_sources = 10
    n_mics = args.mics
    n_sources_target = args.srcs  # the determined case
    if args.algo.startswith("ogive") or args.algo == "five":
        print("IVE only works with a single source. Using only one source.")
        n_sources_target = 1

    use_fake_blinky = False
    use_real_R = False

    # fix the randomness for repeatability
    np.random.seed(30)

    # set the source powers, the first one is half
    source_std = np.ones(n_sources_target)
    source_std[0] /= np.sqrt(2.0)

    SINR = 5  # signal-to-interference-and-noise ratio
    SINR_diffuse_ratio = 0.99  # ratio of uncorrelated to diffuse noise

    # STFT parameters
    framesize = 4096
    hop = framesize // 2
    win_a = pra.hamming(framesize)
    win_s = pra.transform.compute_synthesis_window(win_a, hop)

    # algorithm parameters
    n_iter = args.n_iter

    # param ogive
    ogive_mu = 0.1
    ogive_update = "switching"
    ogive_iter = 4000

    # Geometry of the room and location of sources and microphones
    room_dim = np.array([10, 7.5, 3])
    mic_locs = semi_circle_layout(
        [4.1, 3.76, 1.2], np.pi, 0.20, n_mics, rot=np.pi / 2.0 * 0.99
    )

    target_locs = semi_circle_layout(
        [4.1, 3.755, 1.1], np.pi / 2, 2.0, n_sources_target, rot=0.743 * np.pi
    )
    # interferer_locs = grid_layout([3., 5.5], n_sources - n_sources_target, offset=[6.5, 1., 1.7])
    interferer_locs = random_layout(
        [3.0, 5.5, 1.5], n_sources - n_sources_target, offset=[6.5, 1.0, 0.5], seed=1234
    )
    source_locs = np.concatenate((target_locs, interferer_locs), axis=1)

    # Prepare the signals
    wav_files = sampling(
        1, n_sources, f"{samples_dir}/metadata.json", gender_balanced=True, seed=2222
    )[0]
    signals = wav_read_center(wav_files, seed=123)

    # Create the room itself
    room = pra.ShoeBox(room_dim, fs=fs, absorption=absorption, max_order=max_order)

    # Place a source of white noise playing for 5 s
    for sig, loc in zip(signals, source_locs.T):
        room.add_source(loc, signal=sig)

    # Place the microphone array
    room.add_microphone_array(pra.MicrophoneArray(mic_locs, fs=room.fs))

    # compute RIRs
    room.compute_rir()

    # define a callback that will do the signal mix to
    # get a the correct SNR and SIR
    callback_mix_kwargs = {
        "sinr": SINR,
        "diffuse_ratio": SINR_diffuse_ratio,
        "n_src": n_sources,
        "n_tgt": n_sources_target,
        "src_std": source_std,
        "ref_mic": 0,
    }

    def callback_mix(
        premix, sinr=0, diffuse_ratio=0, ref_mic=0, n_src=None, n_tgt=None, src_std=None
    ):

        # first normalize all separate recording to have unit power at microphone one
        p_mic_ref = np.std(premix[:, ref_mic, :], axis=1)
        premix /= p_mic_ref[:, None, None]
        premix[:n_tgt, :, :] *= src_std[:, None, None]

        # Total variance of noise components
        var_noise_tot = 10 ** (-sinr / 10) * np.sum(src_std ** 2)

        # compute noise variance
        sigma_n = np.sqrt((1 - diffuse_ratio) * var_noise_tot)

        # now compute the power of interference signal needed to achieve desired SINR
        sigma_i = np.sqrt((diffuse_ratio / (n_src - n_tgt)) * var_noise_tot)
        premix[n_tgt:n_src, :, :] *= sigma_i

        # Mix down the recorded signals
        mix = np.sum(premix[:n_src, :], axis=0) + sigma_n * np.random.randn(
            *premix.shape[1:]
        )

        return mix

    # Run the simulation
    separate_recordings = room.simulate(
        callback_mix=callback_mix,
        callback_mix_kwargs=callback_mix_kwargs,
        return_premix=True,
    )
    mics_signals = room.mic_array.signals

    print("Simulation done.")

    # Monitor Convergence
    #####################

    ref = np.moveaxis(separate_recordings, 1, 2)
    if ref.shape[0] < n_mics:
        ref = np.concatenate(
            (ref, np.random.randn(n_mics - ref.shape[0], ref.shape[1], ref.shape[2])),
            axis=0,
        )

    SDR, SIR, eval_time = [], [], []

    def convergence_callback(Y, **kwargs):
        global SDR, SIR, ref

        t_enter = time.perf_counter()

        from mir_eval.separation import bss_eval_sources

        # projection back
        z = projection_back(Y, X_mics[:, :, 0])
        Y = Y.copy() * np.conj(z[None, :, :])

        if Y.shape[2] == 1:
            y = pra.transform.synthesis(Y[:, :, 0], framesize, hop, win=win_s)[:, None]
        else:
            y = pra.transform.synthesis(Y, framesize, hop, win=win_s)
        y = y.astype(np.float64)

        if args.algo != "blinkiva":
            new_ord = np.argsort(np.std(y, axis=0))[::-1]
            y = y[:, new_ord]

        m = np.minimum(y.shape[0] - framesize + hop, ref.shape[1])
        sdr, sir, sar, perm = bss_eval_sources(
            ref[:n_sources_target, :m, 0],
            y[framesize - hop : m + framesize - hop, :n_sources_target].T,
        )
        SDR.append(sdr)
        SIR.append(sir)

        t_exit = time.perf_counter()
        eval_time.append(t_exit - t_enter)

    if args.algo.startswith("ogive"):
        callback_checkpoints = list(range(0, ogive_iter, ogive_iter // n_iter))
    else:
        callback_checkpoints = list(range(0, n_iter))
    if args.no_cb:
        callback_checkpoints = []

    # START BSS
    ###########

    # shape: (n_frames, n_freq, n_mics)
    X_all = pra.transform.analysis(mics_signals.T, framesize, hop, win=win_a).astype(
        np.complex128
    )
    X_mics = X_all[:, :, :n_mics]

    tic = time.perf_counter()

    # Run BSS
    if args.algo == "auxiva":
        # Run AuxIVA
        Y = overiva(
            X_mics,
            n_iter=n_iter,
            proj_back=False,
            model=args.dist,
            init_eig=(args.init == init_choices[1]),
            callback=convergence_callback,
            callback_checkpoints=callback_checkpoints,
        )
    elif args.algo == "auxiva_pca":
        # Run AuxIVA
        Y = auxiva_pca(
            X_mics,
            n_src=n_sources_target,
            n_iter=n_iter,
            proj_back=False,
            model=args.dist,
            callback=convergence_callback,
            callback_checkpoints=callback_checkpoints,
        )
    elif args.algo == "overiva":
        # Run AuxIVA
        Y = overiva(
            X_mics,
            n_src=n_sources_target,
            n_iter=n_iter,
            proj_back=False,
            model=args.dist,
            init_eig=(args.init == init_choices[1]),
            callback=convergence_callback,
            callback_checkpoints=callback_checkpoints,
        )
    elif args.algo == "ilrma":
        # Run AuxIVA
        Y = pra.bss.ilrma(
            X_mics,
            n_iter=n_iter,
            n_components=2,
            proj_back=False,
            callback=convergence_callback,
        )
    elif args.algo == "ogive":
        # Run OGIVE
        Y = ogive(
            X_mics,
            n_iter=ogive_iter,
            step_size=ogive_mu,
            update=ogive_update,
            proj_back=False,
            model=args.dist,
            init_eig=(args.init == init_choices[1]),
            callback=convergence_callback,
            callback_checkpoints=callback_checkpoints,
        )
    elif args.algo == "five":
        Y = five(
            X_mics,
            n_iter=n_iter,
            proj_back=False,
            model=args.dist,
            init_eig=(args.init == init_choices[1]),
            callback=convergence_callback,
            callback_checkpoints=callback_checkpoints,
        )
    else:
        raise ValueError("No such algorithm {}".format(args.algo))

    # projection back
    z = projection_back(Y, X_mics[:, :, 0])
    Y *= np.conj(z[None, :, :])

    toc = time.perf_counter()

    tot_eval_time = sum(eval_time)

    print("Processing time: {} s".format(toc - tic - tot_eval_time))
    print("Evaluation time: {} s".format(tot_eval_time))

    # Run iSTFT
    if Y.shape[2] == 1:
        y = pra.transform.synthesis(Y[:, :, 0], framesize, hop, win=win_s)[:, None]
    else:
        y = pra.transform.synthesis(Y, framesize, hop, win=win_s)
    y = y.astype(np.float64)

    if args.algo != "blinkiva":
        new_ord = np.argsort(np.std(y, axis=0))[::-1]
        y = y[:, new_ord]

    # Compare SIR
    #############
    m = np.minimum(y.shape[0] - framesize + hop, ref.shape[1])
    sdr, sir, sar, perm = bss_eval_sources(
        ref[:n_sources_target, :m, 0],
        y[framesize - hop : m + framesize - hop, :n_sources_target].T,
    )

    # reorder the vector of reconstructed signals
    y_hat = y[:, perm]

    print("SDR:", sdr)
    print("SIR:", sir)

    import matplotlib.pyplot as plt

    plt.figure()

    for i in range(n_sources_target):
        plt.subplot(2, n_sources_target, i + 1)
        plt.specgram(ref[i, :, 0] + 1e-1, NFFT=1024, Fs=room.fs)
        plt.title("Source {} (clean)".format(i))

        plt.subplot(2, n_sources_target, i + n_sources_target + 1)
        plt.specgram(y_hat[:, i], NFFT=1024, Fs=room.fs)
        plt.title("Source {} (separated)".format(i))

    plt.tight_layout(pad=0.5)

    plt.figure()
    a = np.array(SDR)
    b = np.array(SIR)
    for i, (sdr, sir) in enumerate(zip(a.T, b.T)):
        plt.plot(callback_checkpoints, sdr, label="SDR Source " + str(i), marker="*")
        plt.plot(callback_checkpoints, sir, label="SIR Source " + str(i), marker="o")
    plt.legend()
    plt.tight_layout(pad=0.5)

    if not args.gui:
        plt.show()
    else:
        plt.show(block=False)

    if args.save:
        wavfile.write(
            "bss_iva_mix.wav",
            room.fs,
            pra.normalize(mics_signals[0, :], bits=16).astype(np.int16),
        )
        for i, sig in enumerate(y_hat):
            wavfile.write(
                "bss_iva_source{}.wav".format(i + 1),
                room.fs,
                pra.normalize(sig, bits=16).astype(np.int16),
            )

    if args.gui:

        from tkinter import Tk

        # Make a simple GUI to listen to the separated samples
        root = Tk()
        my_gui = PlaySoundGUI(
            root, room.fs, mics_signals[0, :], y_hat.T, references=ref[:, :, 0]
        )
        root.mainloop()
