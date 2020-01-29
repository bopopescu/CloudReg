from cloudvolume import CloudVolume
import numpy as np
from PIL import Image
from tqdm import tqdm
from joblib import Parallel, delayed
from glob import glob
import argparse
import bs4
import time
import tifffile as tf
import PIL

from skimage import transform
import tinybrain

PIL.Image.MAX_IMAGE_PIXELS = None

CHUNK_SIZE = 16

def chunks(l,n):
    for i in range(0,len(l),n):
        yield l[i:i+n]


def create_cloud_volume(precomputed_path,img_size,voxel_size,num_hierarchy_levels=6,parallel=True):
    info = CloudVolume.create_new_info(
        num_channels    = 1,
        layer_type      = 'image',
        data_type       = 'uint16', # Channel images might be 'uint8'
        encoding        = 'raw', # raw, jpeg, compressed_segmentation, fpzip, kempressed
        resolution      = voxel_size, # Voxel scaling, units are in nanometers
        voxel_offset    = [0, 0, 0], # x,y,z offset in voxels from the origin
        # Pick a convenient size for your underlying chunk representation
        # Powers of two are recommended, doesn't need to cover image exactly
        chunk_size      = [ CHUNK_SIZE*8, CHUNK_SIZE*8, CHUNK_SIZE ], # units are voxels
        volume_size     = img_size, # e.g. a cubic millimeter dataset
    )
    vol = CloudVolume(precomputed_path,info=info,parallel=parallel)
    # add mip 1
    [vol.add_scale((2**i,2**i,1)) for i in range(num_hierarchy_levels)]
    vol.commit_info()
    return vol

def get_vol_at_mip(precomputed_path, mip, parallel=True):
    return CloudVolume(precomputed_path,mip=mip,parallel=parallel)


def load_image(path_to_file,transpose=True):
    image = np.squeeze(np.asarray(Image.open(path_to_file)))
    if transpose:
        return image.T
    return image


def load_image_to_array(path_to_file,out_array,z_idx,transpose=True):
    # image = tf.imread(path_to_file)
    image = np.squeeze(np.asarray(Image.open(path_to_file)))
    if transpose:
        out_array[:,:,z_idx] = image.T
    else:
        out_array[:,:,z_idx] = image


def downsample_image(image,out_arrays,num_mips,z_idx,factor=(2,2,1)):
    img_pyramid = tinybrain.downsample_with_averaging(image, factor=factor, num_mips=num_mips)
    #print(f"num mips based on img_pyramid: {len(img_pyramid)}")
    for i in range(1,num_mips):
        out_arrays[i][:,:,z_idx] = img_pyramid[i-1]


def upload_image_to_volume(vol,files):
    size = vol.info['scales'][0]['size']
    num_mips = len(vol.info['scales'])
    vols = [get_vol_at_mip(vol.layer_cloudpath,i,parallel=True) for i in range(num_mips)]
    for i,f in tqdm(enumerate(chunks(files,CHUNK_SIZE)),total=int(len(files)/CHUNK_SIZE)+1):
        tmp_chunk = np.zeros((size[0],size[1],len(f)),dtype='uint16',order='F')
        #s = time.time()
        Parallel(n_jobs=len(f),require='sharedmem')(delayed(load_image_to_array)(img,tmp_chunk,idx) for idx,img in enumerate(f))
        #with tf.TiffSequence(f) as imseq:
        #    imseq.asarray(ioworkers=len(f), out=tmp_chunk.T)
        #print(f"took {time.time() - s} seconds to load {CHUNK_SIZE} slices into memory")
        img_pyramid = tinybrain.accelerated.average_pooling_2x2(tmp_chunk, num_mips)
        start = i*CHUNK_SIZE
        end = start + len(f)
        # time upload at  res 0
        #s = time.time()
        vol[:,:,start:end]=tmp_chunk
        #print(f"took {time.time() - s} seconds to upload {CHUNK_SIZE} slices at res 0")
        for j in range(num_mips-1):
            #s = time.time()
            #print(f'flags: {img_pyramid[j].flags}')
            vols[j+1][:,:,start:end] = img_pyramid[j]
            #print(f"took {time.time() - s} seconds to upload {CHUNK_SIZE} slices at res {j+1}")


def get_image_dims(files):
    # get X,Y size of image by loading first slice
    img = load_image(files[0])
    # get Z size by number of files in directory
    z_size = len(files)
    x_size,y_size = img.shape
    return [x_size,y_size,z_size]

def get_voxel_dims(path_to_xml):
    strainer = bs4.SoupStrainer(name='voxel_dims')
    with open(path_to_xml,'r') as f:
        x = f.read()
    soup = bs4.BeautifulSoup(x,features='html.parser',parse_only=strainer)
    x_size = float(soup.voxel_dims.attrs['v'])*1000
    y_size = float(soup.voxel_dims.attrs['h'])*1000
    z_size = float(soup.voxel_dims.attrs['d'])*1000
    return [x_size,y_size,z_size]

def parallel_assign_image(array,idx,image):
    array[:,:,idx] = image

def main():
    parser = argparse.ArgumentParser(description='Convert local volume into precomputed volume on S3.')
    parser.add_argument('input_path',help='Path to directory containing stitched tiles named sequentially.')
    parser.add_argument('input_xml',help='Path to xml_import.xml file to get metadata')
    parser.add_argument('precomputed_path',help='Path to location on s3 where precomputed volume should be stored. Example: s3://<bucket>/<experiment>/<channel>')
    parser.add_argument('--extension',help='Extension of stitched files. default is tif', default='tif',type=str)
    args = parser.parse_args()
    
    files = np.sort(glob(f'{args.input_path}/*.{args.extension}')).tolist()
    print(f'input path: {args.input_path}')
    img_size = get_image_dims(files)
    voxel_size = get_voxel_dims(args.input_xml)
    print(f'image size is: {img_size}')
    print(f'voxel size is: {voxel_size}')
    vol = create_cloud_volume(args.precomputed_path,img_size,voxel_size)
    # create list of CloudVolume objects at each mip
    upload_image_to_volume(vol,files)

if __name__ == "__main__":
    main()